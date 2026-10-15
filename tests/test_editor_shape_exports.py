"""The edited JavaScript schema reaches the real HWPX/HTML API exporters."""
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from fastapi.testclient import TestClient
import app
from converter.hwpx import NS


class HtmlObjects(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class EditorShapeExportTests(unittest.TestCase):
    def test_editor_fixture_preserves_vector_and_cell_styles_in_real_exports(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is needed for the real editor state fixture')
        root = Path(__file__).resolve().parents[1]
        temporary_root = root / 'tmp/editor-shape-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as folder:
            work = Path(folder)
            fixture = work / 'edited.json'
            done = subprocess.run([node, str(root / 'tests/test_editor_shapes.cjs'), '--export-fixture', str(fixture)],
                                  cwd=root, capture_output=True, text=True, timeout=20)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            document = json.loads(fixture.read_text(encoding='utf-8'))
            with patch.object(app, 'DATA', work):
                client = TestClient(app.app)
                hwpx = client.post('/api/export', json={'document': document, 'format': 'hwpx'})
                self.assertEqual(hwpx.status_code, 200, hwpx.text[:500] if hwpx.status_code != 200 else '')
                html = client.post('/api/export', json={'document': document, 'format': 'html'})
                self.assertEqual(html.status_code, 200, html.text[:500] if html.status_code != 200 else '')
                link = client.post('/api/export?download=1', json={'document': document, 'format': 'json'})
                self.assertEqual(link.status_code, 200, link.text)
                downloaded = client.get(link.json()['download_url'])
                self.assertEqual(downloaded.status_code, 200)
                project = downloaded.json()['document']
                self.assertEqual(project['pages'][0]['blocks'][0]['height'], 0)
                self.assertTrue(project['pages'][0]['blocks'][3]['text_edited'])
                self.assertEqual(project['pages'][0]['blocks'][3]['source_font'], 'Original Font Alias')
                self.assertEqual(project['pages'][0]['blocks'][3]['model_review']['message'], '원본 확인')
                self.assertEqual(client.get(link.json()['download_url']).status_code, 404)
            with ZipFile(io.BytesIO(hwpx.content)) as archive:
                section = ET.fromstring(archive.read('Contents/section0.xml'))
                header = ET.fromstring(archive.read('Contents/header.xml'))
                line = section.find('.//hp:line', NS)
                self.assertIsNotNone(line)
                self.assertEqual(line.find('hc:endPt', NS).get('y'), '0')
                self.assertEqual(line.find('hp:lineShape', NS).get('color'), '#CC0000')
                self.assertEqual(line.find('hc:endPt', NS).get('x'), '9600')
                self.assertEqual(len(section.findall('.//hp:rect', NS)), 1)
                self.assertEqual(len(section.findall('.//hp:polygon', NS)), 1)
                self.assertEqual(section.findall('.//hp:drawText', NS), [])
                text = document['pages'][0]['blocks'][3]['text']
                self.assertIn(text, ''.join(section.itertext()))
                styles = {item.get('id'): item for item in header.findall('.//hh:borderFill', NS)}
                cells = section.findall('.//hp:tc', NS)
                self.assertEqual(len(cells), 2)
                first = styles[cells[0].get('borderFillIDRef')]
                second = styles[cells[1].get('borderFillIDRef')]
                self.assertIsNone(first.find('hc:fillBrush/hc:winBrush', NS))
                self.assertEqual(second.find('hc:fillBrush/hc:winBrush', NS).get('faceColor'), '#DDEEFF')
                self.assertEqual(first.find('hh:leftBorder', NS).get('color'), '#112233')
            parsed = HtmlObjects()
            parsed.feed(html.content.decode('utf-8'))
            self.assertEqual(sum(tag == 'svg' for tag, _ in parsed.tags), 3)
            self.assertTrue(any(tag == 'polygon' for tag, _ in parsed.tags))
            self.assertTrue(any(tag == 'polyline' and attrs.get('points') == '0,0 200,0' for tag, attrs in parsed.tags))
            td = [attrs for tag, attrs in parsed.tags if tag == 'td']
            self.assertIn('background:transparent', td[0]['style'])
            self.assertIn('border:4px solid #112233', td[0]['style'])
            self.assertIn('background:#DDEEFF', td[1]['style'])


if __name__ == '__main__':
    unittest.main()
