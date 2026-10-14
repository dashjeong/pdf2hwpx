"""Atomic page checkpoints and lazy reads for resumable sequential conversion."""
from collections.abc import Sequence
from pathlib import Path
import hashlib
import json
import os
import time

PIPELINE_VERSION = 2
MAX_PAGE_JSON = 8 * 1024 * 1024


def source_digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows readers/virus scanners can briefly hold a handle without DELETE
    # sharing. Keep the previous complete checkpoint and retry boundedly.
    for attempt in range(30):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(min(.1, .01 * (attempt + 1)))


class StoredPages(Sequence):
    """Load one editable page at a time, optionally replacing edited pages."""
    def __init__(self, folder, records, edits=None):
        self.folder, self.records = Path(folder), records
        self.edits = {int(p['position']): p['page'] for p in (edits or [])}
        if any(i < 0 or i >= len(records) for i in self.edits):
            raise ValueError('편집한 페이지 번호가 올바르지 않습니다.')

    def __len__(self):
        return len(self.records)

    def __getitem__(self, position):
        if isinstance(position, slice):
            return [self[i] for i in range(*position.indices(len(self)))]
        if position < 0:
            position += len(self)
        if position in self.edits:
            return self.edits[position]
        name = self.records[position]['file']
        path = (self.folder / name).resolve()
        if path.parent != self.folder.resolve() or not name.startswith('editable-'):
            raise ValueError('저장한 페이지 경로가 올바르지 않습니다.')
        if path.stat().st_size > MAX_PAGE_JSON:
            raise ValueError('페이지 편집 자료가 너무 큽니다. 변환할 범위를 줄여 주세요.')
        return json.loads(path.read_text(encoding='utf-8'))


class DocumentStore:
    def __init__(self, folder, metadata):
        self.folder = Path(folder)
        self.metadata = metadata

    @classmethod
    def create(cls, source, document, engine, options):
        source = Path(source)
        # Provider keys are request-only. Explicitly persist harmless settings.
        allowed = ('page_range', 'force_ocr', 'model', 'base_url', 'detect_tables', 'cell_ocr',
                   'preserve_images', 'deskew', 'upscale_small_images', 'language', 'threads',
                   'use_angle_classifier', 'restore_source_spaces', 'api_image_max_side',
                   'api_timeout', 'api_max_tokens', 'sllm_review_lines')
        settings = {key: options[key] for key in allowed
                    if key in options and isinstance(options[key], (str, bool, int, float))}
        state = {'pipeline_version': PIPELINE_VERSION, 'source': source.name,
                 'source_sha256': source_digest(source), 'requested_engine': engine,
                 'options': settings, 'status': 'running', 'completed': [], 'document': document}
        store = cls(source.parent, state)
        store.flush()
        return store

    @classmethod
    def open(cls, folder):
        folder = Path(folder)
        path = folder / 'state.json'
        for attempt in range(10):
            try:
                return cls(folder, json.loads(path.read_text(encoding='utf-8')))
            except (PermissionError, FileNotFoundError):
                if attempt == 9:
                    raise
                time.sleep(.02 * (attempt + 1))

    def validate_resume(self):
        if self.metadata.get('pipeline_version') != PIPELINE_VERSION:
            raise ValueError('프로그램 처리 방식이 바뀌었습니다. 원본을 다시 변환해 주세요.')
        source = (self.folder / self.metadata['source']).resolve()
        if source.parent != self.folder.resolve() or source_digest(source) != self.metadata['source_sha256']:
            raise ValueError('원본 파일이 바뀌었습니다. 새 변환을 시작해 주세요.')
        expected_prefix = '/api/assets/' + str(self.metadata['document']['id']) + '/'
        indices = set()
        for page in self.pages():
            if page['index'] in indices:
                raise ValueError('저장한 페이지 번호가 중복됩니다. 원본을 다시 변환해 주세요.')
            indices.add(page['index'])
            urls = [page.get('image_url')]
            urls += [block.get('image_url') for block in page.get('blocks', []) if block.get('type') == 'image']
            for url in urls:
                if not isinstance(url, str) or not url.startswith(expected_prefix):
                    raise ValueError('완료한 페이지의 원본 이미지 자료를 찾을 수 없습니다.')
                asset = (self.folder / url[len(expected_prefix):]).resolve()
                if asset.parent != self.folder.resolve() or asset.suffix.lower() != '.png' or not asset.is_file():
                    raise ValueError('완료한 페이지의 이미지 자료가 사라졌습니다. 원본을 다시 변환해 주세요.')
        return source

    def flush(self):
        atomic_json(self.folder / 'state.json', self.metadata)

    def save_page(self, page):
        position = len(self.metadata['completed'])
        name = f'editable-{position + 1:04d}.json'
        encoded = json.dumps(page, ensure_ascii=False)
        if len(encoded.encode('utf-8')) > MAX_PAGE_JSON:
            raise ValueError('한 페이지의 편집 자료가 너무 큽니다. 작은 범위로 다시 변환해 주세요.')
        atomic_json(self.folder / name, page)
        summary = {key: page[key] for key in ('index', 'width', 'height', 'dpi', 'image_url', 'recognition_source') if key in page}
        confidence = [b['confidence'] for b in page.get('blocks', []) if isinstance(b.get('confidence'), (int, float))]
        summary['stats'] = {'blocks': len(page.get('blocks', [])), 'confidence_sum': sum(confidence),
                            'confidence_count': len(confidence),
                            'characters': sum(len(b.get('text', '')) if b.get('type') == 'text' else
                                              sum(len(str(c)) for r in b.get('rows', []) for c in r)
                                              if b.get('type') == 'table' else 0 for b in page.get('blocks', [])),
                            'tables': sum(b.get('type') == 'table' for b in page.get('blocks', [])),
                            'images': sum(b.get('type') == 'image' for b in page.get('blocks', []))}
        self.metadata['completed'].append({'file': name, 'summary': summary})
        self.flush()

    def pages(self, edits=None):
        return StoredPages(self.folder, self.metadata['completed'], edits)

    def document(self, edits=None):
        return {**self.metadata['document'], 'pages': self.pages(edits)}

    def summary(self):
        return {**self.metadata['document'], 'pages': [dict(record['summary'], blocks=[], _loaded=False)
                                                      for record in self.metadata['completed']],
                'lazy_document': True}

    def write_document(self, destination=None, edits=None, wrapped=False):
        destination = Path(destination or self.folder / 'document.json')
        temporary = destination.with_suffix(destination.suffix + '.tmp')
        doc = self.document(edits)
        with temporary.open('w', encoding='utf-8') as stream:
            if wrapped:
                stream.write('{"version":1,"document":')
            metadata = {k: v for k, v in doc.items() if k != 'pages'}
            stream.write(json.dumps(metadata, ensure_ascii=False)[:-1] + ',"pages":[')
            for i, page in enumerate(doc['pages']):
                if i:
                    stream.write(',')
                json.dump(page, stream, ensure_ascii=False)
            stream.write(']}' + ('}' if wrapped else ''))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        return destination
