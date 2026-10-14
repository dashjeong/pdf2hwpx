"""Attest build inputs; prevent publishing an old EXE as newer source."""
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(data)
    return value.hexdigest()


def source_fingerprint():
    paths = [ROOT / name for name in ('app.py', 'run.py', 'requirements.txt', 'requirements.lock.txt',
                                      'install.ps1', 'install.cmd',
                                      'scripts/build_windows.py', 'scripts/build_fingerprint.py', 'scripts/public_bundle.py',
                                      'converter/Skeleton.hwpx')]
    for directory, pattern in (('converter', '*.py'), ('web', '*'), ('models', '*manifest.json')):
        paths.extend((ROOT / directory).glob(pattern))
    for directory in ('models', 'runtime'):
        paths.extend(path for path in (ROOT / directory).rglob('*') if path.is_file())
    return {str(path.relative_to(ROOT)).replace('\\', '/'): digest(path)
            for path in sorted(set(paths)) if path.is_file()}
