"""Audit/stage an explicit public source list without exposing local documents.

No network operations occur in this script. --git-stage adds only the audited
public list to the local index; it does not commit or publish. Use --stage only
after reviewing the path-only audit. Never use a recursive git add on the local
working folder containing private conversion inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    ".gitignore", "LICENSE", "NOTICE", "README.md", "CONTRIBUTING.md",
    "SECURITY.md", "requirements.txt", "requirements.lock.txt", "app.py",
    "run.py", "install.cmd", "install.ps1",
)
DOC_FILES = ("ARCHITECTURE.md", "OCR.md", "GITHUB_RELEASE.md", "VALIDATION_PUBLIC.md", "THIRD_PARTY_LICENSES.md")
SCRIPT_FILES = (
    "public_source.py", "prepare_models.py", "prepare_sllm.py", "build_windows.py", "build_fingerprint.py", "public_bundle.py",
    "benchmark_ocr.py", "package_release.py", "package_smoke.py", "benchmark_sllm.py", "final_bundle_qa.py",
)
DENIED_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp",
    ".hwp", ".docx", ".exe", ".dll", ".pyd", ".so", ".zip", ".7z",
    ".gguf", ".onnx", ".safetensors", ".bin", ".db", ".sqlite", ".pyc",
}
SECRET_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{25,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:[A-Za-z]:[\\/]|/Users/|/home/)[^\r\n]*?(?:Desktop|\.codex|\.aws)[\\/]", re.I),
)


def public_files() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES]
    files += list((ROOT / "converter").glob("*.py"))
    files += [ROOT / "converter" / name for name in (
        "Skeleton.hwpx", "HWPX_TEMPLATE_LICENSE.txt", "HWPX_TEMPLATE_NOTICE.txt")]
    files += [ROOT / "web" / name for name in ("index.html", "app.js", "styles.css")]
    files += list((ROOT / "tests").glob("test_*.py"))
    files += list((ROOT / "tests").glob("test_*.cjs"))
    files += [ROOT / "tests" / "fixtures" / "README.md", ROOT / "samples" / "README.md"]
    files += [ROOT / "scripts" / name for name in SCRIPT_FILES]
    files += [ROOT / "docs" / name for name in DOC_FILES]
    files += [p for p in (ROOT / "licenses").rglob("*") if p.suffix.lower() != ".xml"]
    files += list((ROOT / ".github").rglob("*"))
    return sorted({p for p in files if p.is_file()}, key=lambda p: p.as_posix())


def audit(files: list[Path]) -> list[str]:
    issues = []
    required = [*ROOT_FILES, *("docs/" + n for n in DOC_FILES),
                *("scripts/" + n for n in SCRIPT_FILES), ".github/workflows/ci.yml"]
    for name in required:
        if not (ROOT / name).is_file():
            issues.append("missing required file: " + name)
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix.lower() in DENIED_SUFFIXES:
            issues.append("forbidden file type: " + relative)
        if path.suffix.lower() == ".hwpx" and relative != "converter/Skeleton.hwpx":
            issues.append("unapproved HWPX: " + relative)
        if path.stat().st_size > 10 * 1024 * 1024:
            issues.append("source exceeds 10 MiB: " + relative)
        if path.suffix.lower() == ".hwpx":
            continue
        try:
            value = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            issues.append("unexpected binary source: " + relative)
            continue
        # Path/rule only: never print the matched secret or source excerpt.
        for i, pattern in enumerate(SECRET_PATTERNS, 1):
            if pattern.search(value):
                issues.append(f"sensitive-content rule {i}: {relative}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true", help="Copy reviewed files to public-staging")
    parser.add_argument("--destination", type=Path, default=Path("public-staging"), help="Fresh staging path within this workspace")
    parser.add_argument("--check-index", action="store_true", help="Reject any staged Git path outside the whitelist")
    parser.add_argument("--check-ignore", action="store_true", help="Verify Git excludes private documents and working directories")
    parser.add_argument("--git-stage", action="store_true", help="Add only audited public paths to Git (no commit or network)")
    args = parser.parse_args()
    files = public_files()
    issues = audit(files)
    paths = [p.relative_to(ROOT).as_posix() for p in files]
    if args.check_ignore:
        probes = ["private.pdf", "private.png", "private.jpg", ".env", "private.key",
                  "output/private.html", "data/private.json", "tmp/private.json",
                  "build/generated.py", "dist/app.exe", "models/model.onnx",
                  "runtime/component.dll", "collaboration/private.md", "docs/REAL_SOURCE.md",
                  "docs/VERIFICATION.md", "docs/HWPX_VALIDATION.md"]
        result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                                cwd=ROOT, input=("\n".join(probes) + "\n").encode("utf-8"), capture_output=True)
        if result.returncode not in (0, 1):
            issues.append("Git exclusion probe failed")
        ignored = set(result.stdout.decode("utf-8").splitlines())
        issues += ["Git does not exclude private path: " + p for p in probes if p not in ignored]
    if args.check_index:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
        indexed = set(result.stdout.decode("utf-8").strip("\0").split("\0")) - {""}
        issues += ["unapproved Git path: " + p for p in sorted(indexed - set(paths))]
    if issues:
        print(json.dumps({"ok": False, "issues": issues}, ensure_ascii=False, indent=2))
        return 1
    if args.git_stage:
        subprocess.run(["git", "add", "--", *paths], cwd=ROOT, check=True)
    manifest = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    if args.stage:
        destination = (ROOT / args.destination).resolve()
        if destination == ROOT or ROOT not in destination.parents:
            raise RuntimeError("Staging destination must be a fresh child directory within the workspace")
        if destination.exists():
            raise RuntimeError("Staging destination already exists; review it before replacing it")
        for path in files:
            target = destination / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (ROOT / "tmp").mkdir(exist_ok=True)
        (ROOT / "tmp" / "public-source-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "files": len(paths), "bytes": sum(p.stat().st_size for p in files),
                      "paths": paths}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
