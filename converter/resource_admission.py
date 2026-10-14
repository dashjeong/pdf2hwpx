"""Serialize costly work and measure this app's process tree, separately from RAM estimates."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import threading
from .resources import plan_resources, GiB

_lock = threading.Lock()
_operation = None


class ResourceBusy(RuntimeError):
    pass


@contextmanager
def heavy_operation(name, wait=False):
    global _operation
    if not _lock.acquire(blocking=wait):
        raise ResourceBusy('다른 작업이 메모리를 사용하고 있어요. 변환 또는 저장이 끝난 뒤 다시 눌러 주세요.')
    _operation = name
    try:
        yield
    finally:
        _operation = None
        _lock.release()


def busy_operation():
    return _operation


def process_memory():
    """Sample current parent + descendants; browser RAM is outside this scope."""
    result = {'scope': 'application_and_owned_children', 'measurement_available': False,
              'private_commit_bytes': 0, 'working_set_bytes': 0, 'processes': [],
              'browser_included': False, 'operation': _operation}
    if os.name != 'nt':
        return result
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                       'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                       'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes, kernel.CreateToolhelp32Snapshot.restype = [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return result
    parents = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            parents[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    owned = {os.getpid()}
    while True:
        extra = {pid for pid, (parent, name) in parents.items() if parent in owned and
                 name.lower() not in {'chrome.exe', 'msedge.exe', 'firefox.exe'}} - owned
        if not extra:
            break
        owned |= extra
    for pid in sorted(owned):
        handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            continue
        try:
            counter = Counters()
            counter.cb = ctypes.sizeof(counter)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counter), counter.cb):
                record = {'pid': pid, 'private_commit_bytes': int(counter.PrivateUsage),
                          'working_set_bytes': int(counter.WorkingSetSize)}
                result['processes'].append(record)
                for field in ('private_commit_bytes', 'working_set_bytes'):
                    result[field] += record[field]
        finally:
            kernel.CloseHandle(handle)
    result['measurement_available'] = any(p['pid'] == os.getpid() for p in result['processes'])
    return result


def admission_plan(width=1240, height=1754, use_llm=False):
    current = process_memory()
    plan = plan_resources(width, height, use_llm=use_llm,
                          owned_bytes=current['private_commit_bytes'] if current['measurement_available'] else 0)
    plan['current_process_memory'] = current
    plan['aggregate_enforcement'] = 'estimated admission + sampled stop; decoder/AI child commit caps'
    return plan


def check_measured_budget():
    current = process_memory()
    if current['measurement_available'] and current['private_commit_bytes'] >= 8 * GiB:
        raise RuntimeError('앱의 메모리 사용량이 처리 예산에 도달했어요. 완료한 쪽은 보관했습니다. 다른 프로그램을 닫고 이어서 변환해 주세요.')
    return current
