"""Model process ownership and startup/shutdown failure regressions."""
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

from converter import local_llm


class FakeProcess:
    def __init__(self):
        self.running = True
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated = True
        self.running = False

    def kill(self):
        self.killed = True
        self.running = False

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0


class ModelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.previous = local_llm._process
        self.previous_job = local_llm._process_job
        local_llm._process = None
        local_llm._process_job = None
        self.addCleanup(setattr, local_llm, "_process", self.previous)
        self.addCleanup(setattr, local_llm, "_process_job", self.previous_job)
        job_patch = patch.object(local_llm, "ProcessJob", autospec=True)
        self.job_factory = job_patch.start()
        self.addCleanup(job_patch.stop)
        self.runtime = MagicMock()
        self.runtime.rglob.side_effect = lambda pattern: iter([Path("fake/llama-server.exe")])

    def test_unhealthy_owned_child_is_reaped_before_failed_replacement(self):
        old = FakeProcess()
        local_llm._process = old
        def fail_launch(*args, **kwargs):
            self.assertTrue(old.terminated)
            self.assertEqual(old.wait_calls, 1)
            self.assertIsNone(local_llm._process)
            raise OSError("simulated launch failure")
        with patch.object(local_llm, "healthy", return_value=False), \
             patch.object(local_llm, "bundled_status", return_value={"ready": True}), \
             patch.object(local_llm, "RUNTIME", self.runtime), \
             patch.object(local_llm.subprocess, "Popen", side_effect=fail_launch):
            with self.assertRaisesRegex(RuntimeError, "기본 한국어 OCR"):
                local_llm.ensure_server()
        self.assertFalse(old.running)
        self.assertIsNone(local_llm._process)

    def test_shutdown_serializes_with_child_creation(self):
        child = FakeProcess()
        entered, release, stopping, stopped = [threading.Event() for _ in range(4)]
        failures = []
        def create(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test startup synchronization timed out")
            return child
        def start():
            try:
                local_llm.ensure_server()
            except Exception as exc:
                failures.append(exc)
        def stop():
            stopping.set()
            local_llm.stop_server()
            stopped.set()
        with patch.object(local_llm, "healthy", side_effect=[False, True]), \
             patch.object(local_llm, "bundled_status", return_value={"ready": True}), \
             patch.object(local_llm, "RUNTIME", self.runtime), \
             patch.object(local_llm.subprocess, "Popen", side_effect=create):
            startup = threading.Thread(target=start)
            shutdown = threading.Thread(target=stop)
            startup.start()
            self.assertTrue(entered.wait(2))
            shutdown.start()
            self.assertTrue(stopping.wait(2))
            try:
                self.assertFalse(stopped.wait(.05), "shutdown completed before child ownership was assigned")
            finally:
                release.set()
                startup.join(2)
                shutdown.join(2)
            self.assertFalse(startup.is_alive())
            self.assertFalse(shutdown.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(child.terminated)
        self.assertTrue(stopped.is_set())
        self.assertIsNone(local_llm._process)

    def test_forced_shutdown_reaps_child_after_kill(self):
        child = FakeProcess()
        def wait(timeout=None):
            child.wait_calls += 1
            if child.wait_calls == 1:
                raise subprocess.TimeoutExpired("fake model", timeout)
            return 0
        child.wait = wait
        local_llm._process = child
        local_llm.stop_server()
        self.assertTrue(child.killed)
        self.assertEqual(child.wait_calls, 2)
        self.assertIsNone(local_llm._process)

    def test_foreign_healthy_server_is_not_replaced(self):
        with patch.object(local_llm, "healthy", return_value=True), \
             patch.object(local_llm, "bundled_status", return_value={"ready": True}), \
             patch.object(local_llm.subprocess, "Popen") as create:
            with self.assertRaisesRegex(RuntimeError, "18081"):
                local_llm.ensure_server()
            create.assert_not_called()
        self.assertIsNone(local_llm._process)

    def test_failed_job_assignment_reaps_child_and_preserves_ocr_fallback(self):
        child = FakeProcess()
        self.job_factory.return_value.assign.side_effect = OSError("simulated assignment refusal")
        with patch.object(local_llm, "healthy", return_value=False), \
             patch.object(local_llm, "bundled_status", return_value={"ready": True}), \
             patch.object(local_llm, "RUNTIME", self.runtime), \
             patch.object(local_llm.subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(RuntimeError, "기본 한국어 OCR"):
                local_llm.ensure_server()
        self.assertTrue(child.terminated)
        self.assertEqual(child.wait_calls, 1)
        self.job_factory.return_value.close.assert_called_once()
        self.assertIsNone(local_llm._process)
        self.assertIsNone(local_llm._process_job)


if __name__ == "__main__":
    unittest.main()
