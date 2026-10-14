"""Exercise a frozen offline bundle with generated fixtures and isolated data.

No user documents or credentials are read. Reports go to ignored output/.
Run only after the final build has been announced; source hashes must match it.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
import xml.etree.ElementTree as ET

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_fingerprint import digest, source_fingerprint
from converter.process_ownership import ProcessJob


def process_snapshot(root_pid):
    """Measure only this exact launch and descendants; do not sample browsers."""
    if os.name != 'nt':
        return {'measurement_available': False, 'processes': [],
                'private_commit_bytes': 0, 'working_set_bytes': 0}
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    *[(n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize',
                       'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                       'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes, kernel.CreateToolhelp32Snapshot.restype = [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    parents = {}
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return {'measurement_available': False, 'processes': [], 'private_commit_bytes': 0, 'working_set_bytes': 0}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        exists = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while exists:
            parents[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            exists = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    owned = {root_pid}
    while True:
        extra = {pid for pid, (parent, name) in parents.items() if parent in owned and
                 name.lower() not in {'chrome.exe', 'msedge.exe', 'firefox.exe'}} - owned
        if not extra:
            break
        owned |= extra
    processes = []
    for pid in sorted(owned):
        handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            continue
        try:
            counter = Counters()
            counter.cb = ctypes.sizeof(counter)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counter), counter.cb):
                processes.append({'pid': pid, 'name': parents.get(pid, (None, ''))[1],
                                  'private_commit_bytes': int(counter.PrivateUsage),
                                  'working_set_bytes': int(counter.WorkingSetSize)})
        finally:
            kernel.CloseHandle(handle)
    return {'measurement_available': any(p['pid'] == root_pid for p in processes),
            'processes': processes,
            'private_commit_bytes': sum(p['private_commit_bytes'] for p in processes),
            'working_set_bytes': sum(p['working_set_bytes'] for p in processes)}


def process_alive(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
    finally:
        kernel.CloseHandle(handle)


def port_free(port):
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def bundle_environment(data=None):
    # A source-level test must not make the frozen app borrow workspace models,
    # Python modules, or another installation's llama runtime.
    environment = dict(os.environ)
    for name in ('SCANEDIT_ROOT', 'SCANEDIT_MODEL_DIR', 'SCANEDIT_LLAMA_DIR',
                 'SCANEDIT_DATA', 'PYTHONPATH', 'PYTHONHOME'):
        environment.pop(name, None)
    if data is not None:
        environment['SCANEDIT_DATA'] = str(data)
    return environment


def disk_bytes(folder):
    total = 0
    for path in folder.rglob('*'):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass
    return total


class Sampler:
    def __init__(self, process, data):
        self.process, self.data = process, data
        self.samples = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        while not self.stop_event.is_set():
            value = process_snapshot(self.process.pid)
            value.update(seconds=round(time.monotonic() - self.begin, 3),
                         data_bytes=disk_bytes(self.data), free_disk_bytes=shutil.disk_usage(self.data).free)
            self.samples.append(value)
            self.stop_event.wait(.5)

    def start(self):
        self.begin = time.monotonic()
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        return {'pid': self.process.pid, 'samples': len(self.samples), 'interval_seconds': .5,
                'peak_private_commit_bytes': max((s['private_commit_bytes'] for s in self.samples), default=0),
                'peak_working_set_bytes': max((s['working_set_bytes'] for s in self.samples), default=0),
                'peak_data_bytes': max((s['data_bytes'] for s in self.samples), default=0),
                'minimum_free_disk_bytes': min((s['free_disk_bytes'] for s in self.samples), default=0),
                'scope': 'launched EXE plus its descendants; browser excluded',
                'observed_children': sorted({(p['pid'], p['name']) for s in self.samples
                                            for p in s['processes'] if p['pid'] != self.process.pid})}


def fixture_font(size, bold=False):
    path = Path('C:/Windows/Fonts/malgunbd.ttf' if bold else 'C:/Windows/Fonts/malgun.ttf')
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default(size=size)


def make_fixtures(folder):
    folder.mkdir(parents=True, exist_ok=True)
    width, height = 1240, 1754
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((100, 130), '공문 변환 시험 / MEMO 34567', font=fixture_font(42, True), fill='black')
    draw.text((100, 245), '문서번호: 시험-2026-1014', font=fixture_font(31), fill='black')
    draw.text((100, 325), '교육 대상과 운영 비용을 다음과 같이 안내합니다.', font=fixture_font(29), fill='black')
    xs, ys = [100, 360, 810, 1140], [500 + 110 * i for i in range(5)]
    rows = [['구분', '내용', '비고'], ['대상', '중학생 120명', '총 6회'],
            ['비용', '345,670원', '전액 지원'], ['기한', '2026. 10. 23.', '온라인 접수']]
    for x in xs:
        draw.line((x, ys[0], x, ys[-1]), fill='black', width=3)
    for y in ys:
        draw.line((xs[0], y, xs[-1], y), fill='black', width=3)
    for ri, row in enumerate(rows):
        for ci, text in enumerate(row):
            draw.text((xs[ci] + 20, ys[ri] + 34), text, font=fixture_font(28, ri == 0), fill='black')
    draw.text((100, 1100), '문의: 02-1234-5678 / 작성자: 김하늘', font=fixture_font(30), fill='black')
    draw.rectangle((965, 1430, 1090, 1555), outline='#b33b35', width=5)
    draw.text((989, 1450), '교육', font=fixture_font(31, True), fill='#b33b35')
    draw.text((989, 1500), '지원', font=fixture_font(31, True), fill='#b33b35')
    png, pdf = folder / 'generated-memo.png', folder / 'generated-scan.pdf'
    image.save(png)
    image.save(pdf, 'PDF', resolution=150)
    repeated = folder / 'generated-15-pages.pdf'
    pages = []
    for index in range(15):
        page = image.copy()
        ImageDraw.Draw(page).text((100, 1650), f'PAGE {index + 1:02d} OF 15', font=fixture_font(28), fill='black')
        pages.append(page)
    pages[0].save(repeated, 'PDF', resolution=150, save_all=True, append_images=pages[1:])
    for page in pages:
        page.close()
    image.close()
    # At least four 16M-budget tiles; synthetic text, ruled table and graphic
    # straddle the nominal 3,872-pixel seam. No production content is used.
    giant = Image.new('RGB', (4200, 4200), 'white')
    draw = ImageDraw.Draw(giant)
    draw.text((200, 180), 'BIG IMAGE 12345 / 큰 이미지 시험', font=fixture_font(80, True), fill='black')
    draw.text((3650, 1100), 'SEAM 67890', font=fixture_font(56, True), fill='black')
    for x in (3420, 3680, 3940, 4180):
        draw.line((x, 3400, x, 4180), fill='black', width=5)
    for y in (3400, 3660, 3920, 4180):
        draw.line((3420, y, 4180, y), fill='black', width=5)
    for ri, y in enumerate((3400, 3660, 3920)):
        for ci, x in enumerate((3420, 3680, 3940)):
            draw.text((x + 35, y + 82), str(71 + ri * 3 + ci), font=fixture_font(74), fill='black')
    for row in range(12):
        for col in range(12):
            x, y = 3580 + col * 50, 2020 + row * 50
            color = ('#156dac', '#ef9b33', '#86bb5d')[(row + col) % 3]
            draw.rectangle((x, y, x + 50, y + 50), fill=color)
    huge = folder / 'generated-giant.png'
    huge_bmp = folder / 'generated-giant.bmp'
    giant.save(huge, optimize=False)
    giant.save(huge_bmp)
    giant.close()
    return {'scan': pdf, 'fifteen': repeated, 'huge': huge, 'huge_bmp': huge_bmp, 'png': png}


class BundleQA:
    def __init__(self, args):
        self.args = args
        self.executable = args.exe.resolve()
        self.run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
        self.folder = ROOT / 'output' / ('final-bundle-qa-' + self.run_id)
        self.data = self.folder / 'data'
        self.data.mkdir(parents=True)
        self.report_path = ROOT / 'output' / 'final-bundle-qa.json'
        if args.source_controls_only:
            self.report_path = ROOT / 'output' / 'final-bundle-qa-source-controls.json'
        self.report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'cases': [], 'runs': [],
                       'fixture_scope': 'generated scans only; no private source documents',
                       'limitations': ['Loopback API tests do not replace browser or Hancom visual review.',
                                       'Physical low-RAM 8/4/2 GB PCs and NPU execution are not tested here.',
                                       'No commercial API credentials or provider network calls are used.',
                                       'The machine network adapter is not disabled; local-only engines are exercised.']}
        self.process = self.sampler = self.log = None
        self.base = f'http://127.0.0.1:{args.port}'
        self.client = httpx.Client(base_url=self.base, timeout=180, trust_env=False)

    def persist(self):
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(self, name, function):
        begin = time.monotonic()
        print('START ' + name, flush=True)
        try:
            result = function() or {}
            record = {'name': name, 'passed': True, 'seconds': round(time.monotonic() - begin, 3), **result}
        except Exception as exc:
            record = {'name': name, 'passed': False, 'seconds': round(time.monotonic() - begin, 3),
                      'error': str(exc), 'traceback': traceback.format_exc()}
        self.report['cases'].append(record)
        self.persist()
        print(json.dumps({k: v for k, v in record.items() if k != 'traceback'}, ensure_ascii=False), flush=True)
        return record['passed']

    def provenance(self):
        manifest_path = self.executable.parent / 'build-manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        actual_exe = digest(self.executable)
        if manifest['executable_sha256'] != actual_exe:
            raise AssertionError('Executable does not match build attestation')
        current = source_fingerprint()
        if manifest['source_sha256'] != current:
            changed = sorted(set(current) ^ set(manifest['source_sha256']) |
                             {p for p in current if manifest['source_sha256'].get(p) != current[p]})
            raise AssertionError('Source changed after build: ' + ', '.join(changed))
        self.report['provenance'] = {'executable': str(self.executable.relative_to(ROOT)),
                                     'executable_sha256': actual_exe, 'build_manifest': manifest}
        return {'matching_source_files': len(current), 'executable_sha256': actual_exe}

    def launch(self):
        if not port_free(self.args.port):
            raise RuntimeError('Test port is occupied; no existing process will be terminated')
        self.log = (self.folder / f'launch-{len(self.report["runs"]) + 1}.log').open('ab')
        environment = bundle_environment(self.data)
        self.process = subprocess.Popen([str(self.executable), '--port', str(self.args.port), '--no-browser'],
                                        cwd=self.executable.parent, env=environment,
                                        stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.sampler = Sampler(self.process, self.data)
        self.sampler.start()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'Owned executable exited with {self.process.returncode}')
            try:
                status = self.client.get('/api/status', timeout=5).raise_for_status().json()
                if any(p['pid'] == self.process.pid for p in status['resources']['process_memory']['processes']):
                    self.report['runs'].append({'pid': self.process.pid, 'initial_status': status,
                                                'started_utc': datetime.now(timezone.utc).isoformat()})
                    return status
            except (httpx.HTTPError, KeyError):
                pass
            time.sleep(.2)
        raise RuntimeError('Isolated app did not become ready')

    def stop(self, forced=False):
        if self.process is None:
            return {}
        child_pids = [p['pid'] for p in process_snapshot(self.process.pid)['processes'] if p['pid'] != self.process.pid]
        if self.process.poll() is None:
            if forced:
                self.process.kill()  # Exact Popen handle owned by this script.
            else:
                response = self.client.post('/api/shutdown', json={})
                response.raise_for_status()
            self.process.wait(timeout=20)
        deadline = time.monotonic() + 10
        while any(process_alive(p) for p in child_pids) and time.monotonic() < deadline:
            time.sleep(.1)
        remaining = [p for p in child_pids if process_alive(p)]
        memory = self.sampler.finish()
        if self.report['runs']:
            self.report['runs'][-1].update(memory=memory, forced_stop=forced,
                                            owned_children_remaining=remaining, exit_code=self.process.returncode)
        self.log.close()
        self.process = self.sampler = self.log = None
        if remaining:
            raise AssertionError('Owned children remained after parent stopped: ' + str(remaining))
        return {'owned_children_remaining': remaining, 'memory': memory}

    def job(self, identifier):
        response = self.client.get('/api/jobs/' + identifier, params={'include_document': 'false'})
        response.raise_for_status()
        value = response.json()
        if 'document' in value:
            raise AssertionError('Lazy job endpoint unexpectedly returned full document')
        return value

    def submit(self, source, engine='local', options=None):
        mime = {'.pdf': 'application/pdf', '.bmp': 'image/bmp'}.get(source.suffix, 'image/png')
        with source.open('rb') as stream:
            response = self.client.post('/api/convert', files={'file': (source.name, stream, mime)},
                                        data={'engine': engine, 'options': json.dumps(options or {'force_ocr': True})})
        response.raise_for_status()
        return response.json()['id']

    def wait_job(self, identifier, predicate=None, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.job(identifier)
            if predicate and predicate(value):
                return value
            if value['status'] in {'done', 'error', 'cancelled'}:
                if predicate:
                    raise AssertionError('Job ended before requested checkpoint: ' + str(value))
                if value['status'] != 'done':
                    raise AssertionError('Conversion failed: ' + str(value))
                return value
            time.sleep(.1 if predicate else .3)
        raise TimeoutError('Conversion did not reach acceptance condition')

    def page(self, identifier, position=0):
        return self.client.get(f'/api/jobs/{identifier}/pages/{position}').raise_for_status().json()

    def exported(self, identifier, fmt, edits=None):
        response = self.client.post('/api/export?download=1', json={'document_id': identifier,
                                     'edits': edits or [], 'format': fmt})
        response.raise_for_status()
        metadata = response.json()
        destination = self.folder / f'{identifier}.{fmt}'
        with self.client.stream('GET', metadata['download_url']) as incoming:
            incoming.raise_for_status()
            with destination.open('wb') as stream:
                for block in incoming.iter_bytes(64 * 1024):
                    stream.write(block)
        assert destination.stat().st_size == metadata['bytes']
        # Background unlink can complete just after the response ends.
        pending = self.data / 'exports' / Path(metadata['download_url']).name
        deadline = time.monotonic() + 3
        while pending.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert not pending.exists(), 'Downloaded temporary file was retained'
        assert self.client.get(metadata['download_url']).status_code == 404, 'Expired link did not return 404'
        return destination

    def basic_flow(self):
        self.basic_id = self.submit(self.fixtures['scan'])
        job = self.wait_job(self.basic_id)
        summary = job['document_summary']
        assert summary['lazy_document'] and len(summary['pages']) == 1
        assert all(not p.get('blocks') for p in summary['pages'])
        page = self.page(self.basic_id)
        texts = [b for b in page['blocks'] if b['type'] == 'text']
        tables = [b for b in page['blocks'] if b['type'] == 'table']
        assert texts, 'Generated scan produced no editable text'
        assert tables and tables[0]['rows'], 'Generated ruled table produced no editable table'
        original = ' '.join(b.get('text', '') for b in page['blocks']) + ' '.join(str(c) for t in tables for row in t['rows'] for c in row)
        assert '34567' in original.replace(',', '').replace(' ', ''), 'Source number marker was lost'
        text_edit, table_edit = 'FINAL EDIT 123 한글', '수정 999,999'
        texts[0]['text'], tables[0]['rows'][0][0] = text_edit, table_edit
        edits = [{'position': 0, 'page': page}]
        artifacts = {}
        for fmt in ('hwpx', 'html', 'json'):
            path = self.exported(self.basic_id, fmt, edits)
            artifacts[fmt] = {'bytes': path.stat().st_size, 'sha256': digest(path)}
            if fmt == 'hwpx':
                with zipfile.ZipFile(path) as package:
                    assert package.testzip() is None
                    assert package.read('mimetype') == b'application/hwp+zip'
                    section = ET.fromstring(package.read('Contents/section0.xml'))
                    words = ''.join(section.itertext())
                    assert text_edit in words and table_edit in words, 'HWPX lost text/table edits'
                    assert any(element.tag.rsplit('}', 1)[-1] == 'tbl' for element in section.iter()), 'HWPX lost actual table structure'
            elif fmt == 'html':
                content = path.read_text(encoding='utf-8')
                assert text_edit in content and table_edit in content and '<table' in content
            else:
                saved = json.loads(path.read_text(encoding='utf-8'))
                assert saved['version'] == 1 and len(saved['document']['pages']) == 1
                saved_page = saved['document']['pages'][0]
                assert any(b.get('text') == text_edit for b in saved_page['blocks'])
                assert any(b.get('rows', [[]])[0][0] == table_edit for b in saved_page['blocks'] if b['type'] == 'table')
        return {'page_count': 1, 'editable_text_blocks': len(texts), 'editable_tables': len(tables),
                'source_number_marker_retained': True, 'text_table_edits_retained': True, 'exports': artifacts}

    def concurrent_download_claim(self):
        from concurrent.futures import ThreadPoolExecutor
        metadata = self.client.post('/api/export?download=1', json={'document_id': self.basic_id,
                           'format': 'hwpx'}).raise_for_status().json()
        barrier = threading.Barrier(2)
        def fetch():
            with httpx.Client(base_url=self.base, timeout=30, trust_env=False) as client:
                barrier.wait(timeout=10)
                return client.get(metadata['download_url'])
        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(lambda _: fetch(), range(2)))
        assert sorted(response.status_code for response in responses) == [200, 404]
        response = next(response for response in responses if response.status_code == 200)
        assert len(response.content) == metadata['bytes'], 'Winning download was truncated'
        with zipfile.ZipFile(io.BytesIO(response.content)) as package:
            assert package.testzip() is None
        return {'statuses': [response.status_code for response in responses],
                'successful_body_bytes': metadata['bytes'], 'truncated_body': False}

    def frozen_worker(self):
        folder = self.folder / 'worker'
        folder.mkdir()
        source = folder / 'source.png'
        image = Image.new('RGB', (257, 193), '#35a273')
        ImageDraw.Draw(image).rectangle((0, 0, 30, 30), fill='#ab3241')
        image.save(source)
        image.close()
        work = folder / '.worker-work'
        work.mkdir()
        manifest = {'source': str(source), 'folder': str(folder), 'work': str(work), 'prefix': 'qa-worker',
                    'frame_index': 0, 'max_pixels': 30000, 'budget_bytes': 1024 ** 3,
                    'header': {'width': 257, 'height': 193, 'orientation': 1, 'format': 'PNG', 'dpi': 150},
                    'result_path': str(work / 'result.json'), 'gate': str(work / 'ready')}
        manifest_path = work / 'input.json'
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with ProcessJob(budget_bytes=manifest['budget_bytes']) as ownership:
            process = subprocess.Popen([str(self.executable), '--image-worker', str(manifest_path)],
                                        cwd=self.executable.parent, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        env=bundle_environment(),
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                ownership.assign(process)
                Path(manifest['gate']).write_text('owned', encoding='ascii')
                code = process.wait(timeout=60)
                result = json.loads(Path(manifest['result_path']).read_text(encoding='utf-8'))
                assert code == 0 and not result.get('error'), str(result)
                assert 'libvips' in result['decoder']
                for tile in result['tiles']:
                    with Image.open(tile['path']) as image:
                        assert image.width * image.height <= 30000
                        # Every tile is independently readable from the frozen worker.
                        image.load()
                return {'decoder': result['decoder'], 'bounded_tiles': len(result['tiles']), 'exit_code': code}
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)

    def giant_flow(self, source=None):
        source = source or self.fixtures['huge']
        identifier = self.submit(source, options={'detect_tables': True, 'preserve_images': True})
        job = self.wait_job(identifier, timeout=600)
        assert job['completed_pages'] == 1 and len(job['document_summary']['pages']) == 1
        page = self.page(identifier)
        assert (page['width'], page['height']) == (4200, 4200), 'Original coordinate canvas was lost'
        folder = self.data / identifier
        tile_paths = list(folder.glob('raster-*-tile-*.png'))
        assert len(tile_paths) > 1, 'Large-image pipeline did not use tiles'
        plan = job['document_summary']['processing']['page_memory_plans'][0]
        bound = plan['max_pixels']
        for path in tile_paths:
            with Image.open(path) as image:
                assert image.width * image.height <= bound
        preview_name = Path(page['image_url']).name
        with Image.open(folder / preview_name) as preview:
            assert max(preview.size) <= 2400 and preview.width * preview.height <= 4000000
            preview_size = list(preview.size)
        assert not list(folder.glob('.raster-*')), 'Decoder scratch directory was not cleaned'
        words = ' '.join(b.get('text', '') for b in page['blocks']) + ' '.join(str(c) for b in page['blocks'] if b['type'] == 'table' for row in b['rows'] for c in row)
        assert '12345' in words.replace(' ', ''), 'Main original number marker was lost in tiled OCR'
        tables = [b for b in page['blocks'] if b['type'] == 'table']
        figures = [b for b in page['blocks'] if b['type'] == 'image']
        self.giant_evidence = {'page': page, 'text': words, 'tables': tables, 'figures': figures}
        merged = self.exported(identifier, 'hwpx')
        with zipfile.ZipFile(merged) as package:
            assert package.testzip() is None
            assert len([n for n in package.namelist() if n.startswith('Contents/section') and n.endswith('.xml')]) == 1
        return {'format': source.suffix, 'source_bytes': source.stat().st_size,
                'canvas': [4200, 4200], 'source_pixels': 4200 * 4200, 'tiles': len(tile_paths),
                'tile_pixel_budget': bound, 'preview': preview_size, 'final_pages': 1,
                'editable_tables': len(tables), 'separate_figures': len(figures),
                'seam_marker_observed': '67890' in words.replace(' ', ''),
                'source_number_marker_retained': True, 'hwpx_bytes': merged.stat().st_size,
                'warnings': job['document_summary'].get('warnings', []),
                'seam_note': 'Text, table and graphic cross seam in fixture; complete seam CER/layout requires separate visual review.'}

    def giant_content(self):
        """Keep seam quality separate from successful splitting/streaming."""
        import re
        from collections import Counter
        evidence = self.giant_evidence
        assert '67890' in evidence['text'].replace(' ', ''), 'Numeric text at tile seam was lost'
        assert evidence['tables'], 'Source ruled table did not remain editable'
        numbers = Counter(token for table in evidence['tables'] for row in table['rows'] for cell in row
                          for token in re.findall(r'\b7[1-9]\b', str(cell)))
        expected = Counter({str(value): 1 for value in range(71, 80)})
        assert numbers == expected, f'Seam table lost or duplicated source numbers: expected {dict(expected)}, observed {dict(numbers)}'
        assert evidence['figures'], 'Source graphic did not survive as a separate editable image object'
        return {'seam_text_number_retained': True, 'all_nine_table_values_once': True,
                'table_objects': len(evidence['tables']), 'separate_image_objects': len(evidence['figures']),
                'scope': 'generated fixture markers and actual table/image structures; not general layout 95%'}

    def safe_cancel_resume(self):
        identifier = self.submit(self.fixtures['fifteen'])
        self.wait_job(identifier, lambda value: value.get('completed', 0) >= 1)
        self.client.post(f'/api/jobs/{identifier}/cancel', json={}).raise_for_status()
        stopped = self.wait_job(identifier, lambda value: value['status'] == 'cancelled')
        assert 0 < stopped['completed'] < 15 and stopped['resumable']
        saved_state = json.loads((self.data / identifier / 'state.json').read_text(encoding='utf-8'))
        prior = {record['file']: digest(self.data / identifier / record['file'])
                 for record in saved_state['completed']}
        self.client.post(f'/api/jobs/{identifier}/resume', json={}).raise_for_status()
        finished = self.wait_job(identifier, timeout=360)
        assert finished['completed'] == 15
        for name, sha in prior.items():
            assert digest(self.data / identifier / name) == sha, 'Completed page rewritten during resume'
        indexes = [p['index'] for p in finished['document_summary']['pages']]
        assert indexes == list(range(15)), 'Resumed output contains missing or duplicate pages'
        return {'cancelled_after_pages': len(prior), 'completed_page_hashes_unchanged': True,
                'resumed_pages': 15, 'unique_ordered_page_indices': True}

    def crash_restart_resume(self):
        identifier = self.submit(self.fixtures['fifteen'])
        checkpoint = self.wait_job(identifier, lambda value: value.get('completed', 0) >= 2)
        # Heavy endpoints must reject concurrent work while the conversion owns RAM.
        busy = self.client.post('/api/export?download=1', json={'document_id': self.basic_id, 'format': 'hwpx'})
        assert busy.status_code == 409, 'Export was admitted during conversion'
        saved_state = json.loads((self.data / identifier / 'state.json').read_text(encoding='utf-8'))
        prior = {record['file']: digest(self.data / identifier / record['file'])
                 for record in saved_state['completed']}
        assert len(prior) >= 2 and len(prior) < 15
        stop = self.stop(forced=True)
        self.launch()
        recoverable = self.client.get('/api/jobs').raise_for_status().json()['jobs']
        assert any(j['id'] == identifier and j['can_resume'] for j in recoverable), 'Interrupted job not offered for resume'
        interrupted = self.job(identifier)
        assert interrupted['status'] == 'interrupted'
        self.client.post(f'/api/jobs/{identifier}/resume', json={}).raise_for_status()
        finished = self.wait_job(identifier, timeout=360)
        assert finished['completed'] == 15
        for name, sha in prior.items():
            assert digest(self.data / identifier / name) == sha, 'Already completed page changed after restart'
        indexes = [p['index'] for p in finished['document_summary']['pages']]
        assert indexes == list(range(15)), 'Duplicate, missing or out-of-order resumed page'
        package = self.exported(identifier, 'hwpx')
        with zipfile.ZipFile(package) as archive:
            assert archive.testzip() is None
            sections = [n for n in archive.namelist() if n.startswith('Contents/section') and n.endswith('.xml')]
            assert len(sections) == 15
        return {'completed_before_forced_stop': len(prior), 'saved_page_hashes_unchanged': True,
                'resumed_pages': 15, 'unique_ordered_page_indices': True, 'hwpx_sections': 15,
                'concurrent_export_status': busy.status_code, 'owned_child_cleanup': stop['owned_children_remaining']}

    def local_ai(self):
        if not self.args.with_ai:
            return {'skipped': True, 'reason': 'Use --with-ai for an exclusive model-port lease; not claimed as tested.'}
        if not port_free(18081):
            return {'skipped': True, 'reason': 'Port 18081 is already occupied; foreign model server left untouched.'}
        status = self.client.get('/api/status').raise_for_status().json()
        plan = status['resources']['memory_plan']
        if not status['models']['sllm']['ready']:
            raise AssertionError('Offline AI model/runtime is absent from final bundle')
        result = self.client.post('/api/settings/test', json={'engine': 'sllm', 'options': {}}, timeout=160).raise_for_status().json()
        if not result.get('ok'):
            return {'skipped': True, 'reason': result.get('message'), 'actual_available_bytes': plan.get('available_bytes'),
                    'meaning': 'Actual admission refused model startup; physical low-memory fallback is not asserted.'}
        children = [p for p in process_snapshot(self.process.pid)['processes'] if p['pid'] != self.process.pid]
        assert any('llama-server' in p['name'].lower() for p in children), 'AI response did not come from an owned model child'
        identifier = self.submit(self.fixtures['png'], engine='sllm', options={'detect_tables': True})
        converted = self.wait_job(identifier, timeout=360)
        assert converted['completed'] == 1
        engine = converted['document_summary']['engine']
        stopped = self.stop()
        return {'provider_test': result, 'actual_conversion_engine': engine,
                'owned_model_children': [p['pid'] for p in children if 'llama-server' in p['name'].lower()],
                'model_children_gone_after_shutdown': not stopped['owned_children_remaining'],
                'external_network_required': False}

    def controlled_memory_fallback(self):
        """Source mocks verify fallback decisions, never a physical RAM claim."""
        import importlib
        from unittest.mock import patch
        from converter.resources import GiB, plan_resources
        decisions = []
        for available_gb in (8, 4, 2):
            plan = plan_resources(available_bytes=available_gb * GiB, use_llm=True)
            decisions.append({'mock_available_gb': available_gb, 'allowed': plan['allowed'],
                              'llm_allowed': plan['llm_allowed'], 'budget_bytes': plan['budget_bytes']})
        assert decisions[0]['llm_allowed']
        assert not decisions[1]['llm_allowed'] and not decisions[2]['llm_allowed']
        exhausted = plan_resources(available_bytes=8 * GiB, owned_bytes=int(7.5 * GiB))
        assert not exhausted['allowed'], 'Existing owned memory was not subtracted from 8 GiB cap'
        control_data = self.folder / 'controlled-source-data'
        control_data.mkdir()
        with patch.dict(os.environ, {'SCANEDIT_DATA': str(control_data)}):
            module = importlib.import_module('app')
        identifier = uuid.uuid4().hex
        folder = control_data / identifier
        folder.mkdir()
        source = folder / 'source.png'
        with Image.new('RGB', (1000, 1000), 'white') as image:
            image.save(source)
        module.JOBS[identifier] = {'id': identifier, 'status': 'queued', 'progress': 0, 'completed': 0}
        def two_gb_plan(width=1240, height=1754, use_llm=False):
            return plan_resources(width, height, available_bytes=2 * GiB, use_llm=use_llm)
        recognized = {'engine': 'local', 'blocks': [{'id': 'budget-test', 'type': 'text', 'text': 'BUDGET 123',
                      'x': 10, 'y': 10, 'width': 200, 'height': 30, 'font_size': 24, 'confidence': .99}], 'warnings': []}
        with patch.object(module, 'admission_plan', side_effect=two_gb_plan), \
             patch('converter.local_llm.ensure_server') as start_model, \
             patch('converter.local_llm.stop_server'), \
             patch('converter.ocr.recognize_page', return_value=recognized) as recognize:
            module.convert_job(identifier, source, 'generated-budget-test.png', 'sllm', {})
            start_model.assert_not_called()
            assert recognize.call_args.args[1] == 'local', 'Insufficient AI budget did not fall back to local OCR'
        result = module.get_job(identifier, include_document=False)
        assert result['status'] == 'done' and result['completed'] == 1
        assert any('AI 검수는 수행하지 않았습니다' in warning for warning in result['document_summary']['warnings'])
        return {'test_type': 'mock-controlled source paths; not frozen physical low-memory test',
                'decisions': decisions, 'owned_memory_subtraction': True,
                'ai_start_suppressed': True, 'local_ocr_fallback_completed': True}

    def run(self):
        if self.args.source_controls_only:
            passed = self.check('controlled_source_memory_fallback', self.controlled_memory_fallback)
            self.report.update(source_controls_only=True, passed=passed,
                               finished_utc=datetime.now(timezone.utc).isoformat())
            self.client.close()
            self.persist()
            return 0 if passed else 1
        if not self.check('build_attestation', self.provenance):
            return 1
        self.fixtures = make_fixtures(self.folder / 'fixtures')
        if self.args.prepare_only:
            self.report['prepared_only'] = True
            self.persist()
            return 0
        try:
            self.check('controlled_source_memory_fallback', self.controlled_memory_fallback)
            self.check('frozen_libvips_worker', self.frozen_worker)
            if not self.check('isolated_startup', lambda: {'pid': self.launch() and self.process.pid}):
                return 1
            basic = self.check('scan_lazy_edit_direct_download', self.basic_flow)
            if basic:
                self.check('concurrent_one_time_download_claim', self.concurrent_download_claim)
            if self.check('large_raster_split_single_output', self.giant_flow):
                self.check('large_raster_seam_content_preservation', self.giant_content)
            self.check('large_bmp_split_single_output', lambda: self.giant_flow(self.fixtures['huge_bmp']))
            self.check('safe_cancel_resume', self.safe_cancel_resume)
            if basic:
                self.check('forced_restart_resume', self.crash_restart_resume)
            self.check('offline_local_ai_owned_cleanup', self.local_ai)
        finally:
            if self.process is not None:
                self.check('final_shutdown_owned_cleanup', self.stop)
                if self.process is not None:
                    self.check('emergency_owned_parent_cleanup', lambda: self.stop(forced=True))
            self.client.close()
            self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
            self.report['passed'] = all(case['passed'] for case in self.report['cases'])
            self.persist()
        return 0 if self.report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, default=ROOT / 'dist' / 'ScanEdit' / 'ScanEdit.exe')
    parser.add_argument('--port', type=int, default=18084)
    parser.add_argument('--with-ai', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--source-controls-only', action='store_true')
    args = parser.parse_args()
    if not args.exe.is_file() and not args.source_controls_only:
        parser.error('Build the offline executable before running bundle QA')
    raise SystemExit(BundleQA(args).run())


if __name__ == '__main__':
    main()
