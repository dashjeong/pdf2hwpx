"""Build-time only. Fetch pinned public model/runtime assets; verify model hashes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / 'models'
RUNTIME = ROOT / 'runtime' / 'llama'
REV = '52d6c8ffea26cc873ac5ad116f8631268d7eb503'
REPO = 'Qwen/Qwen3-VL-2B-Instruct-GGUF'
FILES = [
    ('Qwen3VL-2B-Instruct-Q4_K_M.gguf', '089d75c52f4b7ffc56ba998ffc50aae89fcafc755f9e7208aacca281dca6c2ae', 1107409952),
    ('mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf', 'f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82', 445053216),
]
LLAMA_TAG = 'b11609'
LLAMA_URL = f'https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/llama-{LLAMA_TAG}-bin-win-cpu-x64.zip'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def download(url, path, expected=None, size=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (not size or path.stat().st_size == size) and (not expected or digest(path) == expected):
        print(f'Verified: {path.name}', flush=True)
        return
    tmp = path.with_suffix(path.suffix + '.partial')
    req = urllib.request.Request(url, headers={'User-Agent': 'ScanEdit-offline-builder/0.1'})
    with urllib.request.urlopen(req, timeout=120) as response, tmp.open('wb') as out:
        count, last = 0, 0
        while chunk := response.read(4 * 1024 * 1024):
            out.write(chunk)
            count += len(chunk)
            if count - last >= 100 * 1024 * 1024:
                print(f'{path.name}: {count // (1024 * 1024)} MB', flush=True)
                last = count
    if size and tmp.stat().st_size != size:
        raise RuntimeError(f'Size mismatch: {path.name}')
    if expected and digest(tmp) != expected:
        raise RuntimeError(f'SHA256 mismatch: {path.name}')
    tmp.replace(path)
    print(f'Complete: {path.name}', flush=True)


def main():
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(download, f'https://huggingface.co/{REPO}/resolve/{REV}/{name}', MODELS / name, sha, size)
                   for name, sha, size in FILES]
        runtime_zip = ROOT / 'build' / 'llama-win-cpu.zip'
        futures.append(pool.submit(download, LLAMA_URL, runtime_zip))
        for future in futures:
            future.result()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(runtime_zip) as archive:
        for info in archive.infolist():
            dest = (RUNTIME / info.filename).resolve()
            if not dest.is_relative_to(RUNTIME.resolve()):
                raise RuntimeError('Unsafe archive entry')
        archive.extractall(RUNTIME)
    licenses = ROOT / 'licenses'
    download('https://raw.githubusercontent.com/ggml-org/llama.cpp/' + LLAMA_TAG + '/LICENSE', licenses / 'llama.cpp-LICENSE.txt')
    download('https://raw.githubusercontent.com/QwenLM/Qwen3-VL/main/LICENSE', licenses / 'Qwen3-VL-2B-LICENSE.txt')
    (MODELS / 'sllm-manifest.json').write_text(json.dumps({'repository': REPO, 'revision': REV,
        'files': [{'name': n, 'sha256': s, 'size': z} for n, s, z in FILES],
        'runtime': {'version': LLAMA_TAG, 'source': LLAMA_URL, 'sha256': digest(runtime_zip)},
        'verified': True}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
