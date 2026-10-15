"""Create fully offline portable Windows package; no online calls during launch."""
from pathlib import Path
import argparse
import shutil
import subprocess
import sys
import os
import json
from datetime import datetime, timezone
from build_fingerprint import source_fingerprint, digest
from public_bundle import refresh_public_content

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--incremental', action='store_true', help='Reuse PyInstaller analysis; input/output hashes are still checked.')
    args = parser.parse_args(argv)
    source_before = source_fingerprint()
    required = [ROOT / 'models' / 'korean_PP-OCRv5_rec_mobile.onnx', ROOT / 'converter' / 'Skeleton.hwpx', ROOT / 'web' / 'app.js']
    for path in required:
        if not path.is_file():
            raise RuntimeError(f'Missing required asset: {path}')
    # PyInstaller's isolated collectors import native extensions before the
    # application bootstrap; provide the app-local DLLs beside build Python.
    for dll in (ROOT / 'runtime' / 'msvc').glob('*.dll'):
        shutil.copy2(dll, Path(sys.executable).parent / dll.name)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--name', 'ScanEdit', '--onedir', '--windowed',
               '--add-data', f'{ROOT / "web"};web', '--add-data', f'{ROOT / "converter" / "Skeleton.hwpx"};converter',
               '--add-data', f'{ROOT / "models"};models', '--add-data', f'{ROOT / "runtime" / "msvc"};runtime/msvc', '--collect-all', 'rapidocr_onnxruntime',
               '--collect-all', 'onnxruntime', '--collect-all', 'pypdfium2', '--collect-all', 'pypdfium2_raw',
               '--collect-all', 'pyvips', '--hidden-import', '_libvips',
               '--hidden-import', 'uvicorn.logging', '--hidden-import', 'uvicorn.loops.asyncio',
               '--hidden-import', 'uvicorn.protocols.http.h11_impl', '--hidden-import', 'uvicorn.lifespan.on',
               str(ROOT / 'run.py')]
    build_env = {**os.environ, 'PYINSTALLER_CONFIG_DIR': str(ROOT / 'build' / 'pyinstaller-cache')}
    if args.incremental:
        command.remove('--clean')
    subprocess.run(command, cwd=ROOT, check=True, env=build_env)
    bundle = ROOT / 'dist' / 'ScanEdit'
    if (ROOT / 'runtime' / 'llama').exists():
        shutil.copytree(ROOT / 'runtime' / 'llama', bundle / '_internal' / 'runtime' / 'llama', dirs_exist_ok=True)
    refresh_public_content(ROOT, bundle)
    if source_fingerprint() != source_before:
        raise RuntimeError('Source changed during build. Rebuild before packaging this executable.')
    attestation = {'created_utc': datetime.now(timezone.utc).isoformat(), 'source_sha256': source_before,
                   'executable_sha256': digest(bundle / 'ScanEdit.exe')}
    (bundle / 'build-manifest.json').write_text(json.dumps(attestation, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Offline bundle ready: {bundle}', flush=True)


if __name__ == '__main__':
    main()
