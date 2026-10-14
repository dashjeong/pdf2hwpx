"""Isolated libvips decode: sequential RGB spool, then bounded random tiles."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
import time
import traceback


def process_manifest(manifest: dict, diagnostic=None) -> dict:
    diagnostic = diagnostic if diagnostic is not None else {}
    def stage(name):
        diagnostic['stage'] = name
        try:
            (Path(manifest['work']) / 'progress.json').write_text(json.dumps(diagnostic), encoding='utf-8')
        except OSError:
            pass
    stage('import_runtime')
    os.environ["VIPS_CONCURRENCY"] = "2"
    os.environ["VIPS_DISC_THRESHOLD"] = "32m"
    os.environ["TMPDIR"] = manifest["work"]
    from .runtime_bootstrap import configure_native_runtime
    configure_native_runtime()
    import pyvips
    stage('open_source')
    from .large_raster import tile_plan
    pyvips.cache_set_max(0)
    pyvips.cache_set_max_mem(min(32 * 1024 * 1024, manifest["budget_bytes"] // 8))
    pyvips.cache_set_max_files(16)
    frame = manifest["frame_index"]
    header = manifest["header"]
    kwargs = {"access": "sequential", "fail_on": "error"}
    if header["format"] in {"TIFF", "GIF", "WEBP"}:
        kwargs.update(page=frame, n=1)
    elif frame:
        raise ValueError("이 형식의 큰 다중 프레임 이미지는 프레임별 PNG 또는 TIFF로 저장해 주세요.")
    if header['format'] == 'BMP':
        from .bmp_stream import load_bmp
        image = load_bmp(pyvips, manifest['source'])
    else:
        image = pyvips.Image.new_from_file(manifest["source"], **kwargs)
    if image.interpretation != "srgb":
        image = image.colourspace("srgb")
    if image.hasalpha():
        image = image.flatten(background=[255, 255, 255])
    if image.format != "uchar":
        image = image.cast("uchar", shift=True)
    work, folder = Path(manifest["work"]), Path(manifest["folder"])
    raw = work / "source.v"
    stage('spool_rgb')
    image.write_to_file(str(raw))
    stage('open_random_spool')
    image = pyvips.Image.new_from_file(str(raw), access="random")
    orientation = header["orientation"]
    stage('normalize_orientation')
    if orientation != 1:
        image = image.copy()
        image.set_type(pyvips.GValue.gint_type, "orientation", orientation)
        normalized = work / "normalized.v"
        image.autorot().write_to_file(str(normalized))
        image = pyvips.Image.new_from_file(str(normalized), access="random")
    width, height = image.width, image.height
    if (width, height) != (header["width"], header["height"]):
        raise RuntimeError("이미지 프레임의 크기가 사전 검사와 다릅니다.")
    max_pixels = manifest["max_pixels"]
    factor = min(1.0, math.sqrt(min(max_pixels, 4_000_000) / (width * height)), 2400 / max(width, height))
    target_width, target_height = max(1, math.floor(width * factor)), max(1, math.floor(height * factor))
    factor = min(target_width / width, target_height / height)
    preview = image.resize(factor) if factor < 1 else image
    preview_path = folder / (manifest["prefix"] + "-preview.png")
    stage('write_preview')
    preview.pngsave(str(preview_path), strip=True)
    tiles = tile_plan(width, height, max_pixels)
    for index, tile in enumerate(tiles):
        stage(f'write_tile_{index}')
        path = folder / f"{manifest['prefix']}-tile-{index:05d}.png"
        image.crop(tile["x"], tile["y"], tile["width"], tile["height"]).pngsave(str(path), strip=True)
        tile["path"] = str(path)
    stage('outputs_complete')
    return {"width": width, "height": height, "dpi": header["dpi"], "image_path": str(preview_path),
            "preview_width": preview.width, "preview_height": preview.height, "tiles": tiles,
            "warnings": ["큰 이미지를 원본 좌표를 유지한 타일로 나누어 순차 처리합니다. 겹침 영역은 각 타일의 담당 구역으로 중복을 제거합니다.",
                         "비교 화면은 축소 미리보기입니다. 작은 글자와 타일 경계의 표는 원본과 확인해 주세요."],
            "decoder": f"libvips {pyvips.version(0)}.{pyvips.version(1)}.{pyvips.version(2)} isolated disk-backed worker"}


def main(manifest_path=None):
    path = Path(manifest_path or sys.argv[1])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    gate = Path(manifest["gate"])
    deadline = time.monotonic() + 30
    while not gate.exists():
        if time.monotonic() >= deadline:
            Path(manifest['result_path']).write_text(json.dumps({'error': '큰 이미지 처리 준비 시간이 초과되었습니다.',
                 'diagnostic': {'stage': 'ownership_gate_timeout'}}), encoding='utf-8')
            return 2
        time.sleep(.05)
    diagnostic = {'stage': 'owned_ready'}
    try:
        result = process_manifest(manifest, diagnostic)
        code = 0
    except ValueError as exc:
        diagnostic.update(error_type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc(limit=8))
        result = {'error': str(exc)[:300], 'diagnostic': diagnostic}
        code = 1
    except Exception as exc:
        diagnostic.update(error_type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc(limit=8))
        result = {"error": "큰 이미지 파일을 읽지 못했습니다. 파일 손상, 임시 디스크 공간과 메모리 예산을 확인해 주세요.",
                  'diagnostic': diagnostic}
        code = 1
    Path(manifest["result_path"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
