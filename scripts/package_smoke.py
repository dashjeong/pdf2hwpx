"""Verify the frozen app over its own loopback port, no Python inside package."""
from pathlib import Path
import io
import json
import sys
import time
import zipfile
import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    base = 'http://127.0.0.1:' + (sys.argv[1] if len(sys.argv) > 1 else '18082')
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        status = client.get('/api/status').raise_for_status().json()
        with (ROOT / 'samples' / 'official-memo-scanned.pdf').open('rb') as source:
            reply = client.post('/api/convert', files={'file': ('scanned.pdf', source, 'application/pdf')}, data={'engine': 'local', 'options': '{}'}).raise_for_status().json()
        for _ in range(180):
            job = client.get('/api/jobs/' + reply['id']).raise_for_status().json()
            if job['status'] in {'done', 'error'}:
                break
            time.sleep(.5)
        if job['status'] != 'done':
            raise RuntimeError(str(job))
        doc = job['document']
        doc['pages'][0]['blocks'][0]['text'] = '배포본 실제 편집 123 한글'
        exported = client.post('/api/export', json={'document': doc, 'format': 'hwpx'}).raise_for_status().content
        with zipfile.ZipFile(io.BytesIO(exported)) as z:
            if z.read('mimetype') != b'application/hwp+zip':
                raise RuntimeError('Wrong mimetype')
            if '배포본 실제 편집 123 한글' not in z.read('Contents/section0.xml').decode():
                raise RuntimeError('Edited text lost')
        out = ROOT / 'output'
        out.mkdir(exist_ok=True)
        (out / 'packaged-edit.hwpx').write_bytes(exported)
        report = {'port': base, 'status': status, 'conversion_seconds': doc['elapsed_seconds'],
                  'pages': len(doc['pages']), 'blocks': len(doc['pages'][0]['blocks']),
                  'editable_tables': sum(b['type'] == 'table' for b in doc['pages'][0]['blocks']),
                  'edit_roundtrip': True, 'native_viewer_validation': 'separate test required'}
        (out / 'package-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='status'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
