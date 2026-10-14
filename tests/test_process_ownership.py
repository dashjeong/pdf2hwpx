"""Real Windows regressions; terminate only subprocesses created by this test."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from converter.process_ownership import ProcessJob


@unittest.skipUnless(os.name == "nt", "Windows job-object integration")
class ProcessOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def _force_parent_exit(self, owned):
        script = """import subprocess,sys,time
from converter.process_ownership import ProcessJob
job=ProcessJob() if sys.argv[1]=='owned' else None
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
if job: job.assign(child)
print(child.pid,flush=True)
time.sleep(60)
"""
        parent = subprocess.Popen([sys.executable, "-u", "-c", script, "owned" if owned else "unowned"],
                                  cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        ready = threading.Event()
        output = []
        def read_pid():
            output.append(parent.stdout.readline())
            ready.set()
        reader = threading.Thread(target=read_pid, daemon=True)
        reader.start()
        child_handle = None
        try:
            self.assertTrue(ready.wait(10), "dummy parent did not report its child PID")
            self.assertTrue(output[0].strip().isdigit(), "dummy parent failed before reporting child")
            # This PID is obtained only from the child that this known parent
            # just created. Retaining a handle prevents targeting a reused PID.
            child_handle = self.kernel.OpenProcess(0x00100000 | 0x0001, False, int(output[0]))
            self.assertTrue(child_handle)
            self.assertEqual(self.kernel.WaitForSingleObject(child_handle, 0), 258)
            parent.kill()  # Force-kill only this test's own Popen parent.
            parent.wait(timeout=5)
            result = self.kernel.WaitForSingleObject(child_handle, 3000 if owned else 150)
            return result
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if child_handle:
                if self.kernel.WaitForSingleObject(child_handle, 0) == 258:
                    self.kernel.TerminateProcess(child_handle, 1)
                    self.kernel.WaitForSingleObject(child_handle, 3000)
                self.kernel.CloseHandle(child_handle)
            parent.stdout.close()
            parent.stderr.close()
            reader.join(timeout=1)

    def test_unowned_child_reproduces_orphan_after_forced_parent_exit(self):
        self.assertEqual(self._force_parent_exit(False), 258)

    def test_job_owned_child_exits_after_forced_parent_exit(self):
        self.assertEqual(self._force_parent_exit(True), 0)

    def test_job_commit_budget_prevents_large_allocation(self):
        script = """import sys
sys.stdin.readline()
try:
 value=bytearray(64*1024*1024)
 print('allocated',flush=True)
except MemoryError:
 print('budget-blocked',flush=True)
"""
        with ProcessJob(budget_bytes=32 * 1024 * 1024) as job:
            child = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                job.assign(child)
                stdout, stderr = child.communicate(b"allocate\n", timeout=10)
                self.assertEqual(child.returncode, 0, stderr.decode(errors="replace"))
                self.assertEqual(stdout.strip(), b"budget-blocked")
            finally:
                if child.poll() is None:
                    job.close()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
