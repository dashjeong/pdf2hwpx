"""One-time download links must never expose a file deleted by another GET."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import app


class DownloadClaimTests(unittest.TestCase):
    def test_concurrent_gets_receive_one_complete_body_and_one_404(self):
        root = Path(__file__).resolve().parents[1] / 'build'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            data = Path(temporary)
            exports = data / 'exports'
            exports.mkdir()
            filename = 'd' * 32 + '.json'
            expected = (b'0123456789' * 131072) + b'END'
            (exports / filename).write_bytes(expected)
            barrier = threading.Barrier(2)
            def download():
                barrier.wait(timeout=10)
                return client.get('/api/downloads/' + filename)
            with patch.object(app, 'DATA', data), TestClient(app.app) as client:
                with ThreadPoolExecutor(max_workers=2) as workers:
                    responses = list(workers.map(lambda _: download(), range(2)))
                self.assertEqual(sorted(response.status_code for response in responses), [200, 404])
                successful = next(response for response in responses if response.status_code == 200)
                self.assertEqual(successful.content, expected)
                self.assertEqual(int(successful.headers['content-length']), len(expected))
                self.assertEqual(client.get('/api/downloads/' + filename).status_code, 404)
            self.assertEqual(list(exports.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
