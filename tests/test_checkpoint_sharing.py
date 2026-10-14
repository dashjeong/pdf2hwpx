"""Windows transient sharing failures never hide a live terminal outcome."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import app
from converter.document_store import DocumentStore, atomic_json

ROOT = Path(__file__).resolve().parents[1] / 'tmp' / 'sharing-tests'
ROOT.mkdir(parents=True, exist_ok=True)


class CheckpointSharingTests(unittest.TestCase):
    def test_retries_replace_without_destroying_old_checkpoint(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'state.json'
            atomic_json(path, {'status': 'running'})
            original = Path.replace
            calls = []
            def briefly_locked(source, destination):
                calls.append(1)
                if len(calls) <= 2:
                    self.assertIn('running', path.read_text())
                    raise PermissionError('Windows reader sharing')
                return original(source, destination)
            with patch.object(Path, 'replace', briefly_locked):
                atomic_json(path, {'status': 'error'})
            self.assertEqual(len(calls), 3)
            self.assertIn('error', path.read_text())

    def test_read_retry_and_live_failure_override_stale_running(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            identity = 'a' * 32
            folder = root / identity
            folder.mkdir()
            source = folder / 'source.png'
            source.write_bytes(b'checkpoint source')
            store = DocumentStore.create(source, {'id': identity, 'name': 'fixture', 'warnings': []}, 'local', {})
            original = Path.read_text
            calls = []
            def sharing_read(path, *args, **kwargs):
                if path.name == 'state.json' and not calls:
                    calls.append(1)
                    raise PermissionError('Windows transient read')
                return original(path, *args, **kwargs)
            jobs = {identity: {'id': identity, 'status': 'error', 'error': 'decoder failed', 'progress': 2}}
            with patch.object(Path, 'read_text', sharing_read), patch.object(app, 'DATA', root), patch.object(app, 'JOBS', jobs):
                response = app.get_job(identity, False)
            self.assertEqual(response['status'], 'error')
            self.assertEqual(response['error'], 'decoder failed')
            self.assertTrue(response['can_resume'])
