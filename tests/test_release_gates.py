"""Reproduce stale binary packaging and private evidence inclusion hazards."""
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / 'tmp' / 'release-gate-tests'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / 'scripts'))
import package_release
import build_fingerprint
from public_bundle import refresh_public_content, validate_public_bundle, DOCS, ROOT_FILES


class ReleaseGateTests(unittest.TestCase):
    def test_korean_installer_is_readable_by_windows_powershell_51(self):
        # Windows PowerShell 5.1 treats a BOM-less UTF-8 -File as ANSI,
        # corrupting the Korean shortcut name before COM can save it.
        data = (ROOT / 'install.ps1').read_bytes()
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        self.assertIn("'스캔노트.lnk'", data.decode('utf-8-sig'))

    def test_installer_changes_invalidate_build_fingerprint(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / 'install.ps1').write_bytes(b'installer version one')
            (root / 'install.cmd').write_bytes(b'launcher')
            with patch.object(build_fingerprint, 'ROOT', root):
                first = build_fingerprint.source_fingerprint()
                self.assertEqual(set(first), {'install.ps1', 'install.cmd'})
                (root / 'install.ps1').write_bytes(b'installer version two')
                second = build_fingerprint.source_fingerprint()
            self.assertNotEqual(first['install.ps1'], second['install.ps1'])
            self.assertEqual(first['install.cmd'], second['install.cmd'])

    def test_stale_executable_cannot_inherit_new_source_fingerprint(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            bundle = Path(directory)
            (bundle / 'ScanEdit.exe').write_bytes(b'old executable')
            (bundle / 'build-manifest.json').write_text(json.dumps({
                'source_sha256': {'app.py': 'old source'},
                'executable_sha256': package_release.sha256(bundle / 'ScanEdit.exe')}), encoding='utf-8')
            with patch.object(package_release, 'BUNDLE', bundle), \
                 patch.object(package_release, 'source_fingerprint', return_value={'app.py': 'new source'}):
                with self.assertRaisesRegex(RuntimeError, 'changed after build'):
                    package_release.main([])
            self.assertFalse((bundle / 'release-manifest.json').exists())

    def test_public_bundle_excludes_all_internal_docs_and_sample_inputs(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            bundle = root / 'dist' / 'ScanEdit'
            bundle.mkdir(parents=True)
            for name in ('docs', 'samples', 'licenses', 'converter'):
                (root / name).mkdir()
            for name in DOCS:
                (root / 'docs' / name).write_text('public documentation', encoding='utf-8')
            (root / 'docs' / 'PRIVATE.md').write_text('private content', encoding='utf-8')
            (root / 'samples' / 'README.md').write_text('generate your own examples', encoding='utf-8')
            (root / 'samples' / 'private-input.pdf').write_bytes(b'private')
            (root / 'licenses' / 'upstream.txt').write_text('upstream attribution', encoding='utf-8')
            for name in ROOT_FILES:
                (root / name).write_text('public', encoding='utf-8')
            for name in ('HWPX_TEMPLATE_LICENSE.txt', 'HWPX_TEMPLATE_NOTICE.txt'):
                (root / 'converter' / name).write_text('upstream attribution', encoding='utf-8')
            (bundle / 'collaboration').mkdir()
            (bundle / 'collaboration' / 'review.md').write_text('private', encoding='utf-8')
            refresh_public_content(root, bundle)
            validate_public_bundle(bundle)
            self.assertFalse((bundle / 'collaboration').exists())
            self.assertFalse((bundle / 'docs' / 'PRIVATE.md').exists())
            self.assertFalse((bundle / 'samples' / 'private-input.pdf').exists())
            self.assertEqual((bundle / 'licenses' / 'upstream.txt').read_text(), 'upstream attribution')
