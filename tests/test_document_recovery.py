import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import app
from converter.document_store import DocumentStore
from converter.resource_admission import heavy_operation


class ImmediateWorker:
    def submit(self, function, *args):
        function(*args)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / 'build'
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temporary.name)
        self.identity = 'a' * 32
        self.folder = self.root / self.identity
        self.folder.mkdir()
        self.source = self.folder / 'source.png'
        Image.new('RGB', (100, 100), 'white').save(self.source)
        self.jobs = {self.identity: {'id': self.identity, 'status': 'queued', 'progress': 0}}
        self.batches, self.reads = [], []
        self.plan = {'allowed': True, 'llm_allowed': False, 'budget_bytes': 2 * 1024**3,
                     'max_pixels': 16000000, 'batch_pages': 1}
        self.patches = [patch.object(app, 'DATA', self.root), patch.object(app, 'JOBS', self.jobs),
                        patch.object(app, 'WORKER', ImmediateWorker()), patch.object(app, 'admission_plan', return_value=self.plan),
                        patch.object(app, 'check_measured_budget', return_value={'measurement_available': False}),
                        patch('converter.local_llm.stop_server'), patch('converter.large_raster.raster_header', return_value={'frames': 4}),
                        patch.object(app, 'image_pages', side_effect=self.images)]
        for mocked in self.patches:
            mocked.start()
        self.client = TestClient(app.app)

    def tearDown(self):
        for mocked in reversed(self.patches):
            mocked.stop()
        self.temporary.cleanup()

    def images(self, path, folder, indices, memory_plans):
        self.batches.append(list(indices))
        for index in indices:
            image_path = folder / f'page-{index}.png'
            Image.new('RGB', (100, 100), 'white').save(image_path)
            memory_plans.append(copy.deepcopy(self.plan))
            yield image_path, {'index': index, 'width': 100, 'height': 100, 'dpi': 150,
                               'image_url': f'/api/assets/{self.identity}/{image_path.name}', 'blocks': []}

    def recognized(self, path, *args):
        index = int(path.stem.split('-')[-1])
        self.reads.append(index)
        return {'engine': 'local', 'blocks': [{'type': 'text', 'x': 10, 'y': 10, 'width': 70, 'height': 20,
                                              'text': f'번호 {index + 1} 1234', 'confidence': .99}], 'warnings': []}

    def convert(self):
        app.convert_job(self.identity, self.source, 'fixture.png', 'local', {})

    def test_failure_restart_resume_processes_only_remaining_pages(self):
        def failing(path, *args):
            if path.stem == 'page-2':
                raise RuntimeError('third page failed')
            return self.recognized(path, *args)
        with patch('converter.ocr.recognize_page', side_effect=failing):
            self.convert()
        self.assertEqual(self.jobs[self.identity]['status'], 'error')
        self.assertNotIn('document', self.jobs[self.identity])
        store = DocumentStore.open(self.folder)
        self.assertEqual(len(store.pages()), 2)
        self.assertEqual([p['index'] for p in store.pages()], [0, 1])
        self.jobs.clear()  # A new process has only the disk checkpoints.
        recovered = self.client.get('/api/jobs').json()['jobs']
        self.assertEqual(recovered[0]['completed_pages'], 2)
        with patch('converter.ocr.recognize_page', side_effect=self.recognized):
            reply = self.client.post(f'/api/jobs/{self.identity}/resume', json={})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(self.batches, [[0, 1, 2, 3], [2, 3]])
        self.assertEqual(self.reads, [0, 1, 2, 3])
        final = self.client.get(f'/api/jobs/{self.identity}?include_document=false').json()
        self.assertEqual(final['status'], 'done')
        self.assertNotIn('document', final)
        self.assertEqual(len(final['document_summary']['pages']), 4)
        self.assertTrue(all(not page['blocks'] for page in final['document_summary']['pages']))
        self.assertEqual(self.client.get(f'/api/jobs/{self.identity}/pages/2').json()['index'], 2)

    def test_changed_source_and_missing_checkpoint_asset_reject_resume(self):
        def failing(path, *args):
            if path.stem == 'page-1':
                raise RuntimeError('second page failed')
            return self.recognized(path, *args)
        with patch('converter.ocr.recognize_page', side_effect=failing):
            self.convert()
        original = self.source.read_bytes()
        self.source.write_bytes(original + b'changed')
        reply = self.client.post(f'/api/jobs/{self.identity}/resume', json={})
        self.assertEqual(reply.status_code, 400)
        self.assertIn('바뀌', reply.text)
        self.source.write_bytes(original)
        (self.folder / 'page-0.png').unlink()
        reply = self.client.post(f'/api/jobs/{self.identity}/resume', json={})
        self.assertEqual(reply.status_code, 400)

    def test_cancel_saves_current_page_and_does_not_begin_next(self):
        def cancelling(path, *args):
            result = self.recognized(path, *args)
            app.cancel_job(self.identity)
            return result
        with patch('converter.ocr.recognize_page', side_effect=cancelling):
            self.convert()
        self.assertEqual(self.jobs[self.identity]['status'], 'cancelled')
        self.assertEqual(self.reads, [0])
        self.assertEqual(len(DocumentStore.open(self.folder).pages()), 1)
        self.assertTrue(self.client.get(f'/api/jobs/{self.identity}').json()['can_resume'])

    def test_running_poll_excludes_document_and_lazy_edited_exports_preserve_other_pages(self):
        self.jobs[self.identity].update(status='running', document={'pages': ['unbounded']})
        response = self.client.get(f'/api/jobs/{self.identity}').json()
        self.assertNotIn('document', response)
        with patch('converter.ocr.recognize_page', side_effect=self.recognized):
            self.convert()
        edited = self.client.get(f'/api/jobs/{self.identity}/pages/1').json()
        edited['blocks'][0]['text'] = '수정 5678'
        payload = {'document_id': self.identity, 'edits': [{'position': 1, 'page': edited}], 'format': 'json'}
        reply = self.client.post('/api/export?download=1', json=payload)
        self.assertEqual(reply.status_code, 200, reply.text)
        project = self.client.get(reply.json()['download_url'])
        self.assertEqual(project.status_code, 200)
        document = project.json()['document']
        self.assertEqual(len(document['pages']), 4)
        self.assertEqual(document['pages'][0]['blocks'][0]['text'], '번호 1 1234')
        self.assertEqual(document['pages'][1]['blocks'][0]['text'], '수정 5678')
        self.assertEqual(DocumentStore.open(self.folder).pages()[1]['blocks'][0]['text'], '번호 2 1234')
        payload['format'] = 'html'
        html = self.client.post('/api/export', json=payload)
        self.assertEqual(html.status_code, 200)
        self.assertIn('수정 5678', html.text)
        reference = '\n'.join(page['blocks'][0]['text'] for page in document['pages'])
        quality = self.client.post('/api/quality', json={**payload, 'reference': reference})
        self.assertEqual(quality.status_code, 200, quality.text)
        self.assertEqual(quality.json()['cer'], 0)

    def test_shared_gate_rejects_concurrent_settings_demo_and_export(self):
        with heavy_operation('occupied'):
            self.assertEqual(self.client.post('/api/settings/test', json={'engine': 'local'}).status_code, 409)
            self.assertEqual(self.client.get('/api/demo').status_code, 409)
            self.assertEqual(self.client.post('/api/export', json={}).status_code, 409)

    def test_json_body_limit_is_applied_before_loading_edit_document(self):
        with patch.object(app, 'MAX_JSON_BODY', 32):
            self.assertEqual(self.client.post('/api/quality', json={'reference': 'a' * 100}).status_code, 413)

    def test_provider_key_is_not_in_checkpoint(self):
        doc = {'id': self.identity, 'name': 'sample', 'warnings': []}
        store = DocumentStore.create(self.source, doc, 'openai', {'api_key': 'private-key', 'detect_tables': False})
        self.assertNotIn('private-key', (self.folder / 'state.json').read_text())
        self.assertFalse(store.metadata['options']['detect_tables'])


if __name__ == '__main__':
    unittest.main()
