"""Refresh notices/evidence and archive the complete offline Windows bundle."""
from pathlib import Path
import argparse
from datetime import datetime, timezone
import hashlib
import json
import shutil
import zipfile
from build_fingerprint import source_fingerprint
from public_bundle import refresh_public_content, validate_public_bundle

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'dist' / 'ScanEdit'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='ScanNote-0.1.0-Windows-x64-offline.zip')
    args = parser.parse_args()
    if Path(args.name).name != args.name or not args.name.endswith('.zip') or any(c in args.name for c in '/\\:'):
        parser.error('Archive name must be a plain ZIP filename.')
    if not (BUNDLE / 'ScanEdit.exe').is_file():
        raise RuntimeError('Build the Windows bundle first.')
    attestation_path = BUNDLE / 'build-manifest.json'
    if not attestation_path.is_file():
        raise RuntimeError('Executable has no build attestation. Rebuild before release.')
    attestation = json.loads(attestation_path.read_text(encoding='utf-8'))
    if attestation.get('source_sha256') != source_fingerprint() or attestation.get('executable_sha256') != sha256(BUNDLE / 'ScanEdit.exe'):
        raise RuntimeError('Source or executable changed after build. Rebuild before release.')
    refresh_public_content(ROOT, BUNDLE)
    validate_public_bundle(BUNDLE)
    source_files = [ROOT / 'app.py', ROOT / 'run.py', ROOT / 'requirements.txt',
                    *sorted((ROOT / 'converter').glob('*.py')), *sorted((ROOT / 'web').glob('*'))]
    manifest = {
        'version': '0.1.0', 'platform': 'Windows x64',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'offline_runtime_included': True, 'python_install_required': False,
        'models': ['PP-OCRv5 Korean ONNX', 'Qwen3-VL-2B Q4_K_M + Q8 projector'],
        'source_sha256': {str(p.relative_to(ROOT)).replace('\\', '/'): sha256(p) for p in source_files if p.is_file()},
        'user_source_documents_in_archive': False,
        'quality_scope': 'See docs/VALIDATION_PUBLIC.md; partial reference glyph regions, not whole-page accuracy guarantee.',
        'executable_sha256': attestation['executable_sha256'],
        'build_attestation': 'build-manifest.json',
    }
    (BUNDLE / 'release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    archive_path = ROOT / 'dist' / args.name
    files = [p for p in sorted(BUNDLE.rglob('*')) if p.is_file() and p.relative_to(BUNDLE).parts[0] != 'data']
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
        for path in files:
            archive.write(path, str(path.relative_to(BUNDLE.parent)))
    with zipfile.ZipFile(archive_path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f'Archive verification failed: {bad}')
        names = archive.namelist()
        if any(name.startswith('ScanEdit/data/') or '스마트도시' in name or '스크린샷' in name for name in names):
            raise RuntimeError('Unexpected user document in offline archive.')
    result = {'archive': str(archive_path), 'archive_bytes': archive_path.stat().st_size,
              'uncompressed_bytes': sum(p.stat().st_size for p in files), 'file_count': len(files),
              'sha256': sha256(archive_path), 'zip_integrity': True,
              'user_documents_excluded': True}
    (ROOT / 'dist' / 'release-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
