"""Demand-start bundled CPU vision model; no downloads at runtime."""
from pathlib import Path
import atexit
import os
import subprocess
import sys
import threading
import time
import urllib.request

from .process_ownership import ProcessJob

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
MODEL_DIR = Path(os.environ.get('SCANEDIT_MODEL_DIR', ROOT / 'models'))
RUNTIME = Path(os.environ.get('SCANEDIT_LLAMA_DIR', ROOT / 'runtime' / 'llama'))
_process = None
_process_job = None
# Startup, replacement, timeout cleanup and shutdown share ownership of one
# child. Reentrant locking also permits startup timeout cleanup to call stop.
_lock = threading.RLock()
MODEL = 'Qwen3VL-2B-Instruct-Q4_K_M.gguf'
PROJECTOR = 'mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf'


def bundled_status():
    binaries = list(RUNTIME.rglob('llama-server.exe')) if RUNTIME.exists() else []
    ready = bool(binaries) and (MODEL_DIR / MODEL).is_file() and (MODEL_DIR / PROJECTOR).is_file()
    return {'ready': ready, 'running': bool(_process and _process.poll() is None), 'model': 'Qwen3-VL-2B Q4_K_M',
            'backend': 'CPU', 'npu_verified': False,
            'message': '내장 Qwen3-VL 2B · CPU · 처음 실행 시 잠시 기다려 주세요' if ready else '소형 AI 모델 묶음이 없습니다. 기본 한국어 OCR을 이용해 주세요.'}


def stop_server():
    global _process, _process_job
    with _lock:
        process = _process
        if process is None:
            if _process_job is not None:
                _process_job.close()
                _process_job = None
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                # Reap the child before releasing its handle or starting a
                # replacement. If this fails, retain ownership for a retry.
                process.wait(timeout=5)
        _process = None
        if _process_job is not None:
            _process_job.close()
            _process_job = None


atexit.register(stop_server)


def healthy():
    try:
        with urllib.request.urlopen('http://127.0.0.1:18081/health', timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def ensure_server(budget_bytes=4 * 1024 ** 3):
    global _process, _process_job
    with _lock:
        if _process and _process.poll() is None and healthy():
            return
        if _process is not None:
            # A live but unhealthy child must not become an untracked model
            # process when Popen assigns the next handle (or launch fails).
            stop_server()
        if not bundled_status()['ready']:
            raise RuntimeError('내장 소형 AI 모델을 찾을 수 없습니다. 한국어 OCR 방식을 선택해 주세요.')
        if healthy() and not _process:
            raise RuntimeError('소형 AI 포트 18081이 다른 프로그램에 사용되고 있습니다.')
        executable = next(RUNTIME.rglob('llama-server.exe'))
        args = [str(executable), '-m', str(MODEL_DIR / MODEL),
                '--mmproj', str(MODEL_DIR / PROJECTOR),
                '--host', '127.0.0.1', '--port', '18081', '--alias', 'local-vision',
                '--ctx-size', '4096', '--parallel', '1', '--threads', str(min(4, os.cpu_count() or 2)),
                '--batch-size', '128', '--ubatch-size', '128', '--n-gpu-layers', '0', '--no-mmproj-offload',
                '--image-max-tokens', '1024', '--no-webui', '--sleep-idle-seconds', '120']
        try:
            _process_job = ProcessJob(budget_bytes=budget_bytes)
            _process = subprocess.Popen(args, cwd=executable.parent, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            _process_job.assign(_process)
        except (OSError, RuntimeError):
            # Never continue with an unowned model process if Windows cannot
            # tie it to this application's lifetime.
            stop_server()
            # convert_job catches RuntimeError to preserve the OCR fallback.
            raise RuntimeError('내장 AI 실행 파일을 시작하지 못했습니다. 기본 한국어 OCR로 변환합니다.') from None
        for _ in range(120):
            if _process.poll() is not None:
                stop_server()
                raise RuntimeError('내장 AI 실행에 실패했습니다. CPU 호환성과 모델 파일을 확인해 주세요.')
            if healthy():
                return
            time.sleep(1)
        stop_server()
        raise RuntimeError('내장 AI 준비 시간이 초과되었습니다. 기본 OCR 방식을 선택해 주세요.')
