"""ScanEdit: loopback-only offline document conversion and browser editor."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
import argparse
import asyncio
import copy
import io
import json
import os
import sys
import threading
import time
import uuid
import webbrowser
import shutil

import pypdfium2 as pdfium
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from PIL import Image, ImageOps

from converter.quality import measure, document_text
from converter.resources import plan_resources
from converter.document_store import DocumentStore
from converter.resource_admission import (ResourceBusy, admission_plan, check_measured_budget,
                                         heavy_operation, process_memory)

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)).resolve()
APP_HOME = Path(sys.executable).parent if getattr(sys, 'frozen', False) else ROOT
DATA = Path(os.environ.get('SCANEDIT_DATA', APP_HOME / 'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
os.environ.setdefault('SCANEDIT_ROOT', str(ROOT))
# Only inspect headers before planning. Large raster decode is isolated and
# tiled by libvips; Pillow sees only bounded pages/tiles in the normal path.
Image.MAX_IMAGE_PIXELS = None
JOBS = {}
JOB_LOCK = threading.RLock()
MAX_COMPLETED_JOBS = 3
WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='conversion')
PORT = 18080
MAX_UPLOAD = 1024 * 1024 * 1024
MAX_JSON_BODY = 64 * 1024 * 1024
app = FastAPI(title='스캔노트', docs_url=None, redoc_url=None)


def prune_finished_jobs():
    """Keep recent terminal jobs in RAM; completed documents stay on disk.

    One conversion worker processes insertion-ordered queued jobs. Never evict
    a queued/running job, whose mutable progress state the worker still owns.
    """
    completed = [job_id for job_id, job in list(JOBS.items())
                 if job.get('status') in {'done', 'error', 'cancelled', 'interrupted'}]
    for job_id in completed[:-MAX_COMPLETED_JOBS]:
        JOBS.pop(job_id, None)


@app.middleware('http')
async def loopback_guard(request: Request, call_next):
    if request.url.hostname not in {'localhost', '127.0.0.1', '[::1]', '::1', 'testserver'}:
        return JSONResponse({'detail': '이 PC에서만 사용할 수 있습니다.'}, 403)
    origin = request.headers.get('origin')
    if origin and (urlparse(origin).hostname not in {'localhost', '127.0.0.1', '::1'} or urlparse(origin).port != PORT):
        return JSONResponse({'detail': '다른 웹사이트의 접근을 차단했습니다.'}, 403)
    if request.method == 'POST' and request.headers.get('sec-fetch-site') == 'cross-site':
        return JSONResponse({'detail': '다른 웹사이트의 요청을 차단했습니다.'}, 403)
    try:
        limit = MAX_JSON_BODY if 'application/json' in request.headers.get('content-type', '') else MAX_UPLOAD + 1024 * 1024
        if int(request.headers.get('content-length', 0)) > limit:
            if limit == MAX_JSON_BODY:
                return JSONResponse({'detail': '편집 자료가 너무 큽니다. 페이지별 저장을 사용해 주세요.'}, 413)
            return JSONResponse({'detail': '파일은 1GB 이내로 올려 주세요.'}, 413)
    except ValueError:
        return JSONResponse({'detail': '잘못된 요청입니다.'}, 400)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    return response


async def bounded_json(request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_JSON_BODY:
            raise HTTPException(413, '편집 자료가 너무 큽니다. 페이지별 저장을 사용해 주세요.')
        chunks.append(chunk)
    try:
        value = json.loads(b''.join(chunks) or b'{}')
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, '설정 또는 편집 자료 형식을 확인해 주세요.')


@app.get('/api/status')
def status():
    from converter.ocr import local_status
    from converter.local_llm import bundled_status
    local = local_status()
    llm = bundled_status()
    return {'version': '0.1.1', 'engines': [
        {'id': 'local', 'label': '내 PC · 한국어 OCR', 'available': local.get('ready', False), 'detail': '한국어 PP-OCRv5 / CPU / 인터넷 불필요'},
        {'id': 'sllm', 'label': '내 PC · 소형 AI', 'available': llm['ready'], 'detail': llm['message']},
        {'id': 'openai', 'label': 'OpenAI API', 'available': True, 'detail': 'API 키와 인터넷 필요'},
        {'id': 'gemini', 'label': 'Gemini API', 'available': True, 'detail': 'API 키와 인터넷 필요'},
        {'id': 'ollama', 'label': 'Ollama / 호환 서버', 'available': True, 'detail': '실행 중인 비전 모델 서버 필요'}],
        'models': {'ocr': local, 'sllm': llm},
        'resources': {'max_upload_mb': 1024, 'max_pages': 500, 'parallel_pages': 1, 'ram_budget_gb': 8,
                      'memory_plan': admission_plan(), 'process_memory': process_memory(), 'automatic_split_merge': True,
                      'checkpoint_resume': True, 'memory_scope': 'app and owned children; browser excluded',
                      'npu': '이 PC에서 검증되지 않음'}}


def save_page(image, folder, index, dpi=150, max_pixels=16000000):
    image = ImageOps.exif_transpose(image).convert('RGB')
    if image.width * image.height > max_pixels:
        ratio = (max_pixels / (image.width * image.height)) ** .5
        image = image.resize((int(image.width * ratio), int(image.height * ratio)), Image.Resampling.LANCZOS)
        dpi *= ratio
    path = folder / f'page-{index + 1}.png'
    image.save(path, optimize=False)
    return path, {'index': index, 'width': image.width, 'height': image.height,
                  'dpi': dpi, 'image_url': f'/api/assets/{folder.name}/{path.name}', 'blocks': []}


def selected_pages(spec, total):
    if not str(spec or '').strip():
        return list(range(total))
    result = set()
    try:
        for part in str(spec).replace(' ', '').split(','):
            if '-' in part:
                first, last = map(int, part.split('-'))
            else:
                first = last = int(part)
            if first < 1 or last < first or last > total:
                raise ValueError()
            result.update(range(first - 1, last))
    except (ValueError, TypeError):
        raise ValueError(f'변환할 쪽은 1~{total} 사이에서 입력해 주세요. 예: 4-5,7-8,13')
    return sorted(result)


def image_pages(path, folder, pages=None, memory_plans=None):
    if path.suffix.lower() == '.pdf':
        pdf = pdfium.PdfDocument(str(path))
        try:
            indices = pages if pages is not None else list(range(len(pdf)))
            if len(indices) > 500:
                raise ValueError('500쪽 이내로 나눠 변환해 주세요.')
            for i in indices:
                page = pdf[i]
                w, h = page.get_size()
                plan = admission_plan(round(w * 150 / 72), round(h * 150 / 72))
                if not plan['allowed']:
                    page.close()
                    raise ValueError(plan['reason'])
                if memory_plans is not None:
                    memory_plans.append(plan)
                scale = min(150 / 72, (plan['max_pixels'] / max(1, w * h)) ** .5)
                bitmap = page.render(scale=scale)
                try:
                    result = save_page(bitmap.to_pil(), folder, i, scale * 72, plan['max_pixels'])
                finally:
                    bitmap.close()
                    page.close()
                yield result
        finally:
            pdf.close()
    else:
        from converter.large_raster import raster_header
        header = raster_header(path)
        indices = pages if pages is not None else list(range(header['frames']))
        if len(indices) > 500:
            raise ValueError('이미지는 500쪽 이내로 나눠 주세요.')
        for i in indices:
            header = raster_header(path, i)
            plan = admission_plan(header['width'], header['height'])
            if not plan['allowed']:
                raise ValueError(plan['reason'])
            if memory_plans is not None:
                memory_plans.append(plan)
            if header['width'] * header['height'] > plan['max_pixels']:
                from converter.large_raster import prepare_raster_tiles
                prepared = prepare_raster_tiles(path, folder, i, plan['max_pixels'], plan['budget_bytes'])
                preview = Path(prepared['image_path'])
                page = {'index': i, 'width': prepared['width'], 'height': prepared['height'],
                        'dpi': prepared['dpi'], 'image_url': f'/api/assets/{folder.name}/{preview.name}',
                        'blocks': [], '_tiles': prepared['tiles'], '_tile_warnings': prepared.get('warnings', [])}
                yield preview, page
                continue
            with Image.open(path) as im:
                im.seek(i)
                yield save_page(im.copy(), folder, i, header['dpi'], plan['max_pixels'])


def recognize_tiles(page, engine, options, job):
    """Recognize bounded tiles serially and join source-coordinate evidence."""
    from converter.ocr import recognize_page
    from converter.tile_merge import merge_tile_results
    tiles = page.pop('_tiles')
    records, warnings = [], page.pop('_tile_warnings', [])
    for tile_index, tile in enumerate(tiles):
        if job.get('cancel_requested'):
            raise InterruptedError('변환을 멈췄어요. 완료한 페이지는 보관했습니다.')
        job['message'] = f'큰 이미지 분할 처리 {tile_index + 1}/{len(tiles)} · 한 조각씩 읽는 중'
        result = recognize_page(Path(tile['path']), engine, {**options, 'upscale_small_images': False})
        records.append({'tile': tile, 'result': result})
    return merge_tile_results(page, records, engine, warnings)


def convert_job(job_id, path, name, engine, options, resume=False):
    with heavy_operation('conversion', wait=True):
        _convert_job(job_id, Path(path), name, engine, options, resume)


def _convert_job(job_id, path, name, engine, options, resume=False):
    from converter.ocr import recognize_page
    job = JOBS[job_id]
    begin = time.monotonic()
    store = DocumentStore.open(path.parent) if resume else None
    doc = store.metadata['document'] if store else {'id': job_id, 'name': name, 'engine': engine, 'warnings': [], 'quality': {}}
    doc.pop('pages', None)
    native_pdf = None
    recognition_engine = engine
    original_options = options
    try:
        if store is None:
            store = DocumentStore.create(path, doc, engine, options)
        if job.get('cancel_requested'):
            raise InterruptedError('변환을 멈췄어요. 원본 파일은 보관했습니다.')
        job.update(status='running', progress=2, message='가용 메모리를 확인하고 분할 계획을 준비하는 중')
        if engine != 'sllm':
            from converter.local_llm import stop_server
            stop_server()
        from converter.local_llm import bundled_status
        resident_ai = engine == 'sllm' and bundled_status()['running']
        initial_plan = admission_plan(use_llm=engine == 'sllm' and not resident_ai)
        if resident_ai and initial_plan['allowed']:
            initial_plan['llm_allowed'] = True
        if not initial_plan['allowed']:
            raise ValueError(initial_plan['reason'])
        if not doc.get('processing'):
            doc['processing'] = {'automatic_split_merge': True, 'initial_plan': initial_plan,
                                 'chunks': [], 'page_memory_plans': [], 'temporary_images_on_disk': True,
                                 'final_files': 'merged', 'checkpoint_resume': True,
                                 'budget_scope': 'application_and_owned_children; browser excluded',
                                 'aggregate_enforcement': 'estimated admission and sampled stop'}
        if engine == 'sllm':
            from converter.local_llm import ensure_server
            try:
                if not initial_plan['llm_allowed']:
                    raise RuntimeError('AI 메모리 예산 부족')
                ensure_server()
                options = {**options, 'base_url': 'http://127.0.0.1:18081/v1', 'model': 'local-vision'}
            except RuntimeError:
                from converter.local_llm import stop_server
                stop_server()
                recognition_engine = 'local'
                doc['warnings'].append('가용 메모리 또는 소형 AI 준비 상태를 고려해 한국어 OCR로 변환했습니다. AI 검수는 수행하지 않았습니다.')
        if path.suffix.lower() == '.pdf':
            pdf = pdfium.PdfDocument(str(path))
            try:
                total = len(pdf)
            finally:
                pdf.close()
        else:
            from converter.large_raster import raster_header
            total = raster_header(path)['frames']
        if not total:
            raise ValueError('빈 문서입니다.')
        if engine == 'local' and path.suffix.lower() == '.pdf' and not options.get('force_ocr', False):
            native_pdf = pdfium.PdfDocument(str(path))
        indices = selected_pages(options.get('page_range', ''), total)
        total = len(indices)
        if total > 500:
            raise ValueError('500쪽 이내로 범위를 지정해 주세요.')
        if store is None:
            store = DocumentStore.create(path, doc, engine, options)
            store.metadata.update(selected_indices=indices, total=total)
        else:
            if indices != store.metadata.get('selected_indices', indices):
                raise ValueError('변환할 페이지 범위가 바뀌었습니다. 새 변환을 시작해 주세요.')
            store.validate_resume()
            store.metadata.update(selected_indices=indices, total=total)
        store.metadata.update(status='running', error=None)
        store.flush()
        completed = {record['summary']['index'] for record in store.metadata['completed']}
        remaining = [index for index in indices if index not in completed]
        job.update(total=total, completed=len(completed), resumable=False)
        memory_plans = []
        images = iter(image_pages(path, path.parent, remaining, memory_plans))
        while True:
            if job.get('cancel_requested'):
                raise InterruptedError('변환을 멈췄어요. 완료한 페이지는 보관했습니다.')
            try:
                page_path, page = next(images)
            except StopIteration:
                break
            i = len(store.metadata['completed'])
            job.update(progress=int(5 + 85 * i / max(1, total)), message=f'{i + 1}/{total}쪽 · 한 쪽씩 읽고 보관하는 중')
            result = None
            if '_tiles' in page:
                result = recognize_tiles(page, recognition_engine, options, job)
            elif native_pdf is not None:
                from converter.native_pdf import recognize_native_pdf_page
                native_page = native_pdf[page['index']]
                try:
                    result = recognize_native_pdf_page(native_page, page_path, options)
                finally:
                    native_page.close()
            if result is None:
                result = recognize_page(page_path, recognition_engine, {**options,
                    'upscale_small_images': memory_plans[-1]['budget_bytes'] >= 2 * 1024 ** 3})
            page['recognition_source'] = result.get('engine', engine)
            page['blocks'] = result.get('blocks', [])
            for block in page['blocks']:
                if block.get('type') == 'image' and block.get('image_path'):
                    crop = Path(block.pop('image_path')).resolve()
                    if crop.parent == page_path.parent.resolve():
                        block['image_url'] = f'/api/assets/{job_id}/{crop.name}'
                if block.get('type') == 'image' and not block.get('image_url'):
                    x, y = max(0, int(block.get('x', 0))), max(0, int(block.get('y', 0)))
                    w, h = max(1, int(block.get('width', 1))), max(1, int(block.get('height', 1)))
                    crop_name = f'crop-{i}-{uuid.uuid4().hex[:8]}.png'
                    with Image.open(page_path) as source:
                        source.crop((x, y, min(source.width, x + w), min(source.height, y + h))).save(path.parent / crop_name)
                    block['image_url'] = f'/api/assets/{job_id}/{crop_name}'
            doc['warnings'].extend(result.get('warnings', []))
            doc['warnings'] = list(dict.fromkeys(doc['warnings']))
            plan = memory_plans[-1] if memory_plans else admission_plan(page['width'], page['height'])
            doc['processing']['page_memory_plans'].append(plan)
            doc['processing']['chunks'].append({'number': i + 1, 'pages': 1, 'checkpoint': f'editable-{i + 1:04d}.json'})
            store.save_page(page)
            job.update(completed=i + 1)
            del page, result
            measured = check_measured_budget()
            doc['processing']['last_measured_memory'] = measured
        job.update(progress=93, message='분할 처리 결과를 하나의 편집 문서로 결합하는 중')
        doc['elapsed_seconds'] = round(doc.get('elapsed_seconds', 0) + time.monotonic() - begin, 2)
        doc['warnings'] = list(dict.fromkeys(doc['warnings']))
        summaries = [record['summary'] for record in store.metadata['completed']]
        confidence_count = sum(page['stats']['confidence_count'] for page in summaries)
        confidence_sum = sum(page['stats']['confidence_sum'] for page in summaries)
        doc['quality'] = {'mean_confidence': confidence_sum / confidence_count if confidence_count else None,
                          'measured': False, 'target_accuracy': .95, 'requires_reference': True}
        sources = {p['recognition_source'] for p in summaries}
        if 'native' in sources:
            doc['engine'] = 'PDF 원본 글자' if sources == {'native'} else 'PDF 원본 글자 + 한국어 OCR'
        elif engine == 'sllm':
            doc['engine'] = '한국어 OCR + 소형 AI 검수' if recognition_engine == 'sllm' else '한국어 OCR (AI 검수 실행 불가)'
        store.write_document()
        store.metadata.update(status='done', progress=100, error=None)
        store.flush()
        job.update(status='done', progress=100, message='변환 완료 · 확인하고 편집해 주세요', resumable=False)
    except InterruptedError as exc:
        job.update(status='cancelled', message=str(exc), resumable=store is not None)
        if store is not None:
            store.metadata.update(status='cancelled', progress=job.get('progress', 0), error=None)
            store.flush()
    except Exception as exc:
        # API adapters sanitize errors; never log raw requests or keys.
        job.update(status='error', message='변환을 멈췄어요. 완료한 페이지는 보관했습니다.', error=str(exc)[:500], resumable=store is not None)
        if store is not None:
            store.metadata.update(status='error', progress=job.get('progress', 0), error=str(exc)[:500])
            store.flush()
    finally:
        if native_pdf is not None:
            native_pdf.close()
        if 'images' in locals() and hasattr(images, 'close'):
            images.close()
        job.pop('document', None)
        options.clear()
        original_options.clear()


@app.post('/api/convert')
async def convert(file: UploadFile = File(...), engine: str = Form('local'), options: str = Form('{}')):
    if engine not in {'local', 'openai', 'gemini', 'ollama', 'sllm'}:
        raise HTTPException(400, '인식 방식을 다시 선택해 주세요.')
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in {'.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}:
        raise HTTPException(400, 'PDF 또는 PNG/JPG/TIFF/BMP/WebP 이미지를 선택해 주세요.')
    try:
        opts = json.loads(options)
        if not isinstance(opts, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, '설정 형식을 확인해 주세요.')
    if sum(j['status'] in {'queued', 'running'} for j in JOBS.values()) >= 3:
        raise HTTPException(429, '현재 변환이 끝난 뒤 다시 시도해 주세요.')
    job_id = uuid.uuid4().hex
    folder = DATA / job_id
    folder.mkdir()
    path = folder / ('source' + suffix)
    size = 0
    try:
        with path.open('wb') as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, '파일은 1GB 이내로 올려 주세요.')
                stream.write(chunk)
        if not size:
            raise HTTPException(400, '빈 파일입니다.')
    except Exception:
        path.unlink(missing_ok=True)
        raise
    prune_finished_jobs()
    JOBS[job_id] = {'id': job_id, 'name': Path(file.filename).name, 'status': 'queued', 'progress': 0, 'completed': 0,
                    'message': '변환을 기다리는 중'}
    WORKER.submit(convert_job, job_id, path, Path(file.filename).name, engine, opts)
    return {'id': job_id}


def job_folder(job_id):
    if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise HTTPException(404, '변환 작업을 찾을 수 없습니다.')
    return DATA / job_id


def open_store(job_id):
    try:
        return DocumentStore.open(job_folder(job_id))
    except (OSError, ValueError, KeyError):
        raise HTTPException(404, '저장한 변환 자료를 찾을 수 없습니다.')


@app.get('/api/jobs')
def recoverable_jobs():
    records = []
    for folder in DATA.iterdir():
        if not folder.is_dir() or len(folder.name) != 32:
            continue
        try:
            job = get_job(folder.name, include_document=False)
        except HTTPException:
            continue
        if job.get('resumable'):
            record = {key: job.get(key) for key in ('id', 'name', 'status', 'completed', 'total', 'message')}
            record.update(can_resume=True, completed_pages=record['completed'], engine=open_store(folder.name).metadata['requested_engine'])
            records.append(record)
    return {'jobs': records}


@app.get('/api/jobs/{job_id}')
def get_job(job_id: str, include_document: bool = True):
    live = JOBS.get(job_id)
    if live is not None and live.get('status') in {'queued', 'running'}:
        return {key: value for key, value in live.items() if key not in {'document', 'options'}}
    folder = job_folder(job_id)
    if (folder / 'state.json').is_file():
        store = open_store(job_id)
        state = store.metadata
        status = live.get('status', state.get('status', 'interrupted')) if live else state.get('status', 'interrupted')
        if status in {'running', 'queued'} and live is None:
            status = 'interrupted'
        reply = {key: value for key, value in (live or {}).items() if key not in {'document', 'options'}}
        reply.update(id=job_id, name=state['document'].get('name', ''), status=status,
                     completed=len(state['completed']), total=state.get('total', len(state['completed'])),
                     progress=100 if status == 'done' else reply.get('progress', state.get('progress', 0)),
                     resumable=status in {'error', 'cancelled', 'interrupted'},
                     message=reply.get('message', '변환 완료 · 확인하고 편집해 주세요' if status == 'done' else '완료한 페이지를 보관했습니다. 이어서 변환할 수 있어요.'))
        error = live.get('error') if live and live.get('error') else state.get('error')
        if error and status in {'error', 'cancelled', 'interrupted'}:
            reply['error'] = error
        reply['document_summary'] = store.summary()
        reply.update(can_resume=reply['resumable'], completed_pages=reply['completed'])
        saved = folder / 'document.json'
        if include_document and status == 'done' and saved.is_file() and saved.stat().st_size <= MAX_JSON_BODY:
            reply['document'] = json.loads(saved.read_text(encoding='utf-8'))
        return reply
    saved = folder / 'document.json'
    if saved.is_file():
        reply = {'id': job_id, 'status': 'done', 'progress': 100, 'resumable': False}
        if include_document and saved.stat().st_size <= MAX_JSON_BODY:
            reply['document'] = json.loads(saved.read_text(encoding='utf-8'))
        return reply
    if live is not None:
        return dict(live)
    raise HTTPException(404, '변환 작업을 찾을 수 없습니다.')


@app.get('/api/jobs/{job_id}/pages/{position}')
def get_page(job_id: str, position: int):
    store = open_store(job_id)
    if position < 0 or position >= len(store.metadata['completed']):
        raise HTTPException(404, '저장한 페이지를 찾을 수 없습니다.')
    try:
        return store.pages()[position]
    except (OSError, ValueError):
        raise HTTPException(400, '저장한 페이지 자료를 읽지 못했습니다.')


@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    job_folder(job_id)
    job = JOBS.get(job_id)
    if job is None or job.get('status') not in {'queued', 'running'}:
        raise HTTPException(409, '현재 진행 중인 변환이 없습니다.')
    job.update(cancel_requested=True, message='현재 페이지를 보관한 뒤 변환을 멈추는 중')
    return {'ok': True, 'id': job_id}


@app.post('/api/jobs/{job_id}/resume')
async def resume_job(job_id: str, request: Request):
    store = open_store(job_id)
    if JOBS.get(job_id, {}).get('status') in {'queued', 'running'} or store.metadata.get('status') == 'done':
        raise HTTPException(409, '이미 진행 중이거나 완료한 변환입니다.')
    body = await bounded_json(request)
    options = body.get('options', body)
    if not isinstance(options, dict):
        raise HTTPException(400, '설정 형식을 확인해 주세요.')
    stored_options = store.metadata.get('options', {})
    if any(key in options and options[key] != value for key, value in stored_options.items()):
        raise HTTPException(400, '처리 설정이 바뀌었습니다. 새 변환을 시작해 주세요.')
    options = {**stored_options, **options}
    engine = store.metadata['requested_engine']
    if engine in {'openai', 'gemini'} and not str(options.get('api_key') or (options.get('api') or {}).get('api_key') or '').strip():
        raise HTTPException(400, '이어서 변환하려면 API 키를 다시 입력해 주세요. 키는 저장하지 않습니다.')
    try:
        source = await asyncio.to_thread(store.validate_resume)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)[:500])
    if sum(j.get('status') in {'queued', 'running'} for j in JOBS.values()) >= 3:
        raise HTTPException(429, '현재 변환이 끝난 뒤 다시 시도해 주세요.')
    with JOB_LOCK:
        if JOBS.get(job_id, {}).get('status') in {'queued', 'running', 'done'}:
            raise HTTPException(409, '이미 진행 중이거나 완료한 변환입니다.')
        JOBS[job_id] = {'id': job_id, 'name': store.metadata['document'].get('name', ''), 'status': 'queued',
                        'progress': store.metadata.get('progress', 0), 'completed': len(store.metadata['completed']),
                        'total': store.metadata.get('total'), 'message': '완료한 페이지 다음부터 이어서 변환하는 중'}
    WORKER.submit(convert_job, job_id, source, JOBS[job_id]['name'], engine, options, True)
    return {'id': job_id}


@app.get('/api/assets/{job_id}/{filename}')
def asset(job_id: str, filename: str):
    if job_id != 'demo' and (len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id)):
        raise HTTPException(404)
    path = (DATA / job_id / filename).resolve()
    if path.parent != (DATA / job_id).resolve() or path.suffix.lower() != '.png' or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.get('/api/demo')
def demo():
    from converter.demo import create_demo
    try:
        with heavy_operation('demo'):
            return create_demo(DATA)
    except ResourceBusy as exc:
        raise HTTPException(409, str(exc))


@app.post('/api/shutdown')
async def shutdown():
    if any(j['status'] in {'queued', 'running'} for j in JOBS.values()):
        raise HTTPException(409, '문서 변환이 끝난 뒤 종료해 주세요.')
    async def close_later():
        await asyncio.sleep(.8)
        from converter.local_llm import stop_server
        stop_server()
        if getattr(app.state, 'server', None):
            app.state.server.should_exit = True
    asyncio.create_task(close_later())
    return {'ok': True, 'message': '프로그램을 종료합니다. 화면을 닫아 주세요.'}


@app.post('/api/quality')
async def quality(request: Request):
    body = await bounded_json(request)
    try:
        with heavy_operation('quality'):
            if body.get('document_id'):
                doc = open_store(str(body['document_id'])).document(body.get('edits', []))
            else:
                doc = body.get('document', {})
            observed = body['text'] if 'text' in body else await asyncio.to_thread(document_text, doc)
            return await asyncio.to_thread(measure, body.get('reference', ''), observed)
    except ResourceBusy as exc:
        raise HTTPException(409, str(exc))
    except (ValueError, OSError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc))


@app.post('/api/settings/test')
async def settings_test(request: Request):
    from converter.ocr import test_provider
    body = await bounded_json(request)
    engine = body.get('engine', body.get('provider', 'local'))
    options = body.get('options', body)
    try:
        with heavy_operation('settings'):
            if not isinstance(options, dict):
                raise ValueError('설정 형식을 확인해 주세요.')
            if engine == 'sllm':
                from converter.local_llm import ensure_server, bundled_status
                resident_ai = bundled_status()['running']
                plan = admission_plan(use_llm=not resident_ai)
                if resident_ai and plan['allowed']:
                    plan['llm_allowed'] = True
                if not plan['allowed'] or not plan['llm_allowed']:
                    raise RuntimeError(plan['reason'])
                await asyncio.to_thread(ensure_server)
                options = {**options, 'base_url': 'http://127.0.0.1:18081/v1', 'model': 'local-vision'}
            result = await asyncio.to_thread(test_provider, engine, options)
            check_measured_budget()
            return result
    except ResourceBusy as exc:
        raise HTTPException(409, str(exc))
    except (ValueError, RuntimeError) as exc:
        return {'ok': False, 'message': str(exc)[:500]}


@app.post('/api/export')
async def export(request: Request):
    from converter.hwpx import export_hwpx_file, export_html_file
    destination = None
    try:
        with heavy_operation('export'):
            body = await bounded_json(request)
            store = open_store(str(body['document_id'])) if body.get('document_id') else None
            edits = body.get('edits', [])
            if not isinstance(edits, list) or any(not isinstance(edit, dict) or not isinstance(edit.get('page'), dict)
                                                or not isinstance(edit.get('position'), int) for edit in edits):
                raise ValueError('편집한 페이지 자료 형식을 확인해 주세요.')
            doc = store.document(edits) if store else body.get('document')
            if not isinstance(doc, dict) or not 0 < len(doc.get('pages', [])) <= 500:
                raise ValueError('변환한 문서를 먼저 열어 주세요.')
            fmt = body.get('format', 'hwpx')
            if fmt not in {'hwpx', 'html', 'json'}:
                raise ValueError('지원하지 않는 파일 형식입니다.')
            folder = DATA / 'exports'
            folder.mkdir(exist_ok=True)
            for stale in folder.glob('*'):
                if stale.is_file() and time.time() - stale.stat().st_mtime > 3600:
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            destination = folder / f'{uuid.uuid4().hex}.{fmt}'
            if fmt == 'hwpx':
                await asyncio.to_thread(export_hwpx_file, doc, DATA, destination)
            elif fmt == 'html':
                await asyncio.to_thread(export_html_file, doc, DATA, destination)
            elif store:
                await asyncio.to_thread(store.write_document, destination, edits, True)
            else:
                def write_project():
                    with destination.open('w', encoding='utf-8') as stream:
                        json.dump({'version': 1, 'document': doc}, stream, ensure_ascii=False)
                await asyncio.to_thread(write_project)
            check_measured_budget()
            if request.query_params.get('download') == '1':
                return {'download_url': f'/api/downloads/{destination.name}', 'bytes': destination.stat().st_size}
            return FileResponse(destination, media_type={'hwpx': 'application/hwp+zip', 'html': 'text/html; charset=utf-8',
                                                         'json': 'application/json'}[fmt], filename=f'converted.{fmt}',
                                background=BackgroundTask(destination.unlink, missing_ok=True))
    except ResourceBusy as exc:
        raise HTTPException(409, str(exc))
    except (ValueError, OSError, TypeError, KeyError, RuntimeError) as exc:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))


@app.get('/api/downloads/{filename}')
def download_file(filename: str):
    identifier, dot, suffix = filename.partition('.')
    if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier) or suffix not in {'hwpx', 'html', 'json'}:
        raise HTTPException(404, '저장할 파일을 찾을 수 없습니다.')
    path = DATA / 'exports' / filename
    # Claim a one-time link before creating response headers. Competing GETs
    # cannot start streaming a path that the first response is about to delete.
    claimed = path.with_name(f'claimed-{uuid.uuid4().hex}.{suffix}')
    with JOB_LOCK:
        if not path.is_file():
            raise HTTPException(404, '저장 링크가 만료됐습니다. 저장 버튼을 다시 눌러 주세요.')
        try:
            path.replace(claimed)
        except OSError:
            raise HTTPException(404, '저장 링크가 만료됐습니다. 저장 버튼을 다시 눌러 주세요.')
    return FileResponse(claimed, media_type={'hwpx': 'application/hwp+zip', 'html': 'text/html', 'json': 'application/json'}[suffix],
                        filename=f'converted.{suffix}', background=BackgroundTask(claimed.unlink, missing_ok=True))


app.mount('/', StaticFiles(directory=ROOT / 'web', html=True), name='web')


def main():
    global PORT
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18080)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    PORT = args.port
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=PORT, log_level='warning', access_log=False))
    app.state.server = server
    server.run()


if __name__ == '__main__':
    main()
