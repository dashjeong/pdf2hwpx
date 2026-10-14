"""Copy only reviewed public documentation into an offline distribution."""
from pathlib import Path
import shutil

DOCS = ('ARCHITECTURE.md', 'OCR.md', 'GITHUB_RELEASE.md', 'VALIDATION_PUBLIC.md', 'THIRD_PARTY_LICENSES.md')
ROOT_FILES = ('README.md', 'LICENSE', 'NOTICE', 'CONTRIBUTING.md', 'SECURITY.md',
              'install.cmd', 'install.ps1', 'requirements.lock.txt')


def refresh_public_content(root: Path, bundle: Path):
    # These paths are always literal children of the explicit build directory.
    resolved = bundle.resolve()
    if resolved != (root / 'dist' / 'ScanEdit').resolve():
        raise ValueError('Unexpected offline bundle directory')
    for name in ('docs', 'samples', 'collaboration', 'verification-results'):
        target = resolved / name
        if target.exists():
            shutil.rmtree(target)
    (bundle / 'docs').mkdir(parents=True, exist_ok=True)
    for name in DOCS:
        shutil.copy2(root / 'docs' / name, bundle / 'docs' / name)
    (bundle / 'samples').mkdir(exist_ok=True)
    shutil.copy2(root / 'samples' / 'README.md', bundle / 'samples' / 'README.md')
    shutil.copytree(root / 'licenses', bundle / 'licenses', dirs_exist_ok=True)
    for name in ROOT_FILES:
        shutil.copy2(root / name, bundle / name)
    for name in ('HWPX_TEMPLATE_LICENSE.txt', 'HWPX_TEMPLATE_NOTICE.txt'):
        shutil.copy2(root / 'converter' / name, bundle / 'licenses' / name)


def validate_public_bundle(bundle: Path):
    for name in ('collaboration', 'verification-results', 'data'):
        if (bundle / name).exists():
            # The running app's private data can exist locally, but is excluded
            # by the archive caller. Internal review/evidence must not exist.
            if name != 'data':
                raise ValueError('Private directory in bundle: ' + name)
    actual = {p.name for p in (bundle / 'docs').iterdir() if p.is_file()}
    if actual != set(DOCS):
        raise ValueError('Unapproved offline documentation')
