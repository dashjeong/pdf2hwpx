"""Completed-job cache stays bounded without losing saved editable documents."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app


class JobRetentionTests(unittest.TestCase):
    def test_old_done_document_is_fetched_from_disk_after_pruning(self):
        ids = [f'{index:032x}' for index in range(1, 6)]
        temporary_root = Path(__file__).resolve().parents[1] / 'tmp/job-cache-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='scan-job-cache-', dir=temporary_root) as folder:
            root = Path(folder).resolve()
            self.assertTrue(root.is_relative_to(temporary_root.resolve()))
            document = {'id': ids[0], 'name': '저장한 편집 문서', 'pages': [{'index': 0, 'blocks': []}]}
            saved = root / ids[0]
            saved.mkdir()
            (saved / 'document.json').write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
            jobs = {identity: {'id': identity, 'status': 'done', 'document': {'id': identity}}
                    for identity in ids}
            with patch.object(app, 'JOBS', jobs), patch.object(app, 'DATA', root):
                app.prune_finished_jobs()
                self.assertEqual(list(app.JOBS), ids[-3:])
                restored = app.get_job(ids[0])
                self.assertEqual(restored['status'], 'done')
                self.assertEqual(restored['document'], document)

    def test_running_and_queued_jobs_are_not_pruned(self):
        running = {'id': 'running', 'status': 'running', 'progress': 42}
        queued = {'id': 'queued', 'status': 'queued', 'progress': 0}
        jobs = {'running': running, 'old-error': {'status': 'error'}, 'queued': queued,
                'old-done': {'status': 'done'}, 'recent-1': {'status': 'done'},
                'recent-2': {'status': 'error'}, 'recent-3': {'status': 'done'}}
        with patch.object(app, 'JOBS', jobs):
            app.prune_finished_jobs()
            self.assertIs(app.JOBS['running'], running)
            self.assertIs(app.JOBS['queued'], queued)
            self.assertEqual([key for key, job in app.JOBS.items() if job['status'] in {'done', 'error'}],
                             ['recent-1', 'recent-2', 'recent-3'])


if __name__ == '__main__':
    unittest.main()
