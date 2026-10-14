"""Real CPU VLM smoke test, including child-process Windows peak RAM."""
from pathlib import Path
import ctypes
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from PIL import Image
    from converter import local_llm
    from converter.ocr import recognize_page
    whole_page = len(sys.argv) > 1
    sample = Path(sys.argv[1]) if whole_page else ROOT / 'build' / 'sllm-crop.png'
    sample.parent.mkdir(exist_ok=True)
    if not whole_page:
        with Image.open(ROOT / 'samples' / 'official-memo.png') as source:
            source.crop((80, 195, 1140, 285)).save(sample)
    begin = time.monotonic()
    local_llm.ensure_server()
    loaded = time.monotonic()
    result = recognize_page(sample, 'sllm', {'base_url': 'http://127.0.0.1:18081/v1', 'model': 'local-vision',
                                           'api_max_tokens': 2048 if whole_page else 512, 'api_timeout': 300, 'image_max_side': 1280})
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(int(local_llm._process._handle)), ctypes.byref(counters), counters.cb)
    report = {'model': local_llm.bundled_status(), 'fixture': str(sample) if whole_page else 'synthetic Korean memo title crop, not whole page',
              'load_seconds': round(loaded - begin, 2), 'inference_seconds': round(time.monotonic() - loaded, 2),
              'peak_working_set_mb': round(counters.PeakWorkingSetSize / 1024**2, 1), 'result': result}
    (ROOT / 'output' / ('sllm-full-page.json' if whole_page else 'sllm-smoke.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'result'}, ensure_ascii=False), flush=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
