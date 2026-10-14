"""Windows child-tree ownership and optional committed-memory budget.

The noninheritable job handle stays in the parent. Windows closes it even on a
forced parent exit, terminating assigned processes and their descendants.
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class ProcessJob:
    """Own only explicitly assigned Popen children; close terminates their tree."""
    def __init__(self, budget_bytes: int | None = None):
        if os.name != "nt":
            raise RuntimeError("Windows 작업 프로세스 관리가 필요합니다.")
        if budget_bytes is not None and (not isinstance(budget_bytes, int) or budget_bytes <= 0):
            raise ValueError("프로세스 메모리 예산이 올바르지 않습니다.")
        self._handle = None
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        # NULL security attributes create a handle children do not inherit.
        handle = self._kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if budget_bytes is not None:
            limits.BasicLimitInformation.LimitFlags |= 0x00000200  # JOB_MEMORY
            limits.JobMemoryLimit = budget_bytes
        if not self._kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process) -> None:
        if not self._handle:
            raise RuntimeError("종료된 프로세스 작업에 연결할 수 없습니다.")
        # Popen retains this Windows process handle for its own wait/terminate.
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise RuntimeError("생성한 Windows 자식 프로세스 핸들이 없습니다.")
        if not self._kernel.AssignProcessToJobObject(self._handle, int(process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle = self._handle
        if handle:
            if not self._kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
