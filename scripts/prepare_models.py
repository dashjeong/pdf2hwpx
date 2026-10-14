"""Build-time model preparation. Never downloads at application runtime."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from converter.runtime_bootstrap import configure_native_runtime

configure_native_runtime()
KOREAN_MODEL = {
    "filename": "korean_PP-OCRv5_rec_mobile.onnx",
    "url": "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx",
    "sha256": "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b",
    "license": "Apache-2.0 (PaddleOCR / RapidAI)",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url: str, destination: Path, expected: str | None = None) -> None:
    if destination.exists() and (not expected or digest(destination) == expected):
        return
    temp = destination.with_suffix(destination.suffix + ".download")
    request = urllib.request.Request(url, headers={"User-Agent": "pdf2hwpx-build/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, temp.open("wb") as output:
        shutil.copyfileobj(response, output)
    if expected and digest(temp) != expected:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Model checksum mismatch: {destination.name}")
    temp.replace(destination)


def prepare(destination: Path, offline: bool = False) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.find_spec("rapidocr_onnxruntime")
    if not spec or not spec.origin:
        raise RuntimeError("Install requirements.txt before preparing models.")
    package = Path(spec.origin).parent
    assets = []
    for name in ("ch_PP-OCRv4_det_infer.onnx", "ch_ppocr_mobile_v2.0_cls_infer.onnx"):
        source = package / "models" / name
        if not source.exists():
            raise RuntimeError(f"Required RapidOCR wheel asset absent: {name}")
        target = destination / name
        if not target.exists() or digest(target) != digest(source):
            shutil.copy2(source, target)
        assets.append({"filename": name, "sha256": digest(target), "bytes": target.stat().st_size,
                       "source": "rapidocr-onnxruntime==1.4.4 wheel", "license": "Apache-2.0"})
    target = destination / KOREAN_MODEL["filename"]
    if offline:
        if not target.exists() or digest(target) != KOREAN_MODEL["sha256"]:
            raise RuntimeError("Verified Korean model missing in offline mode.")
    else:
        fetch(KOREAN_MODEL["url"], target, KOREAN_MODEL["sha256"])
    assets.append({**KOREAN_MODEL, "bytes": target.stat().st_size})
    for repository, filename in (("RapidAI/RapidOCR", "RapidOCR-LICENSE.txt"),
                                 ("PaddlePaddle/PaddleOCR", "PaddleOCR-LICENSE.txt")):
        path = destination / filename
        if not offline:
            fetch(f"https://raw.githubusercontent.com/{repository}/main/LICENSE", path)
        if not path.exists():
            raise RuntimeError(f"License file missing: {filename}")
    # Check metadata without loading inference tensors; this model embeds its dictionary.
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    session = ort.InferenceSession(str(target), sess_options=opts, providers=["CPUExecutionProvider"])
    metadata = session.get_modelmeta().custom_metadata_map
    if "character" not in metadata:
        raise RuntimeError("Recognition model lacks its character dictionary metadata.")
    manifest = {"runtime": "onnxruntime CPU", "recognition": "PP-OCRv5 Korean mobile",
                "embedded_dictionary": True, "assets": assets,
                "sources": ["https://github.com/RapidAI/RapidOCR",
                            "https://www.paddleocr.ai/v3.1.0/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html"]}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=ROOT / "models")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    result = prepare(args.destination, args.offline)
    print(json.dumps({"assets": len(result["assets"]), "total_bytes": sum(a["bytes"] for a in result["assets"]),
                      "dictionary": result["embedded_dictionary"]}))
