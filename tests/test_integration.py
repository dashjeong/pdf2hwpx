"""User-story checks: actual upload -> Korean OCR -> editable export."""
from pathlib import Path
import io
import json
import time
import unittest
import zipfile
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
import app
from converter.demo import prepare_fixtures
from converter.quality import measure

ROOT = Path(__file__).resolve().parents[1]


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prepare_fixtures(ROOT)
        cls.client = TestClient(app.app)

    @unittest.skipUnless(all((ROOT / 'models' / name).is_file() for name in (
        'korean_PP-OCRv5_rec_mobile.onnx', 'ch_PP-OCRv4_det_infer.onnx',
        'ch_ppocr_mobile_v2.0_cls_infer.onnx')), 'Separate Korean OCR models unavailable')
    def test_actual_scanned_pdf_and_edit_export(self):
        with (ROOT / 'samples' / 'official-memo-scanned.pdf').open('rb') as source:
            reply = self.client.post('/api/convert', files={'file': ('memo.pdf', source, 'application/pdf')},
                                     data={'engine': 'local', 'options': '{}'})
        self.assertEqual(reply.status_code, 200, reply.text)
        job_id = reply.json()['id']
        for _ in range(180):
            job = self.client.get('/api/jobs/' + job_id).json()
            if job['status'] in {'done', 'error'}:
                break
            time.sleep(.5)
        self.assertEqual(job['status'], 'done', job)
        doc = job['document']
        blocks = doc['pages'][0]['blocks']
        self.assertTrue(any(b['type'] == 'text' and '2026' in b['text'] for b in blocks))
        self.assertTrue(any(b['type'] == 'table' for b in blocks))
        text = next(b for b in blocks if b['type'] == 'text')
        text['text'] = '수정한 문서 <숫자 123> & 한글'
        exported = self.client.post('/api/export', json={'document': doc, 'format': 'hwpx'})
        self.assertEqual(exported.status_code, 200, exported.text[:500] if exported.status_code != 200 else '')
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            self.assertEqual(archive.read('mimetype'), b'application/hwp+zip')
            sections = [ET.fromstring(archive.read(n)) for n in archive.namelist() if n.startswith('Contents/section')]
            self.assertTrue(any('수정한 문서 <숫자 123> & 한글' in ''.join(s.itertext()) for s in sections))
        out = ROOT / 'output'
        out.mkdir(exist_ok=True)
        (out / 'verified-edit.hwpx').write_bytes(exported.content)
        (out / 'verified-document.json').write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_reference_quality_and_no_fake_score(self):
        self.assertEqual(measure('한글 123', '한글 123')['cer'], 0)
        self.assertGreater(measure('한글 123', '한글 124')['cer'], 0)
        demo = self.client.get('/api/demo').json()
        self.assertFalse(demo['quality']['measured'])
        self.assertIsNone(demo['quality']['mean_confidence'])
        reply = self.client.post('/api/quality', json={'reference': '공문 123', 'text': '공문 124'})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()['character_errors'], 1)

    def test_bad_files_origin_and_assets(self):
        reply = self.client.post('/api/convert', files={'file': ('malware.exe', b'bad')})
        self.assertEqual(reply.status_code, 400)
        reply = self.client.post('/api/quality', headers={'Origin': 'https://example.com'}, json={'reference': 'x', 'text': 'x'})
        self.assertEqual(reply.status_code, 403)
        self.assertEqual(self.client.get('/api/assets/bad/README.md').status_code, 404)
        self.assertEqual(self.client.post('/api/export', json={'document': {'pages': []}}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
