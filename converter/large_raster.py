"""Prepare huge raster sources in an owned, memory-limited decoder process."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import struct
import threading
import time
import uuid

from PIL import Image

_HEADER_LOCK = threading.RLock()


def _exif_orientation(data):
    exif = Image.Exif()
    exif.load(data if data.startswith(b"Exif") else b"Exif\0\0" + data)
    return int(exif.get(274, 1))


def _chunk_header(source):
    """PNG/WebP metadata scan uses seeks; Pillow PNG getexif can decode pixels."""
    with Path(source).open("rb") as stream:
        signature = stream.read(12)
        png = signature[:8] == b"\x89PNG\r\n\x1a\n"
        webp = signature[:4] == b"RIFF" and signature[8:12] == b"WEBP"
        if not png and not webp:
            return None
        stream.seek(8 if png else 12)
        width = height = 0
        orientation, frames = 1, 0
        resolution = None
        while header := stream.read(8):
            if len(header) != 8:
                raise ValueError("이미지 메타데이터가 손상되었습니다.")
            if png:
                size, kind = struct.unpack(">I4s", header)
            else:
                kind, size = struct.unpack("<4sI", header)
            payload_start = stream.tell()
            if kind == b"IHDR":
                width, height = struct.unpack(">II", stream.read(8))
            elif kind == b"pHYs":
                if size != 9:
                    raise ValueError("PNG 해상도 메타데이터가 손상되었습니다.")
                x_pixels, y_pixels, unit = struct.unpack('>IIB', stream.read(9))
                if unit == 1:
                    resolution = (x_pixels * .0254, y_pixels * .0254)
            elif kind == b"acTL":
                frames = struct.unpack(">I", stream.read(4))[0]
            elif kind == b"VP8X":
                data = stream.read(10)
                width, height = int.from_bytes(data[4:7], "little") + 1, int.from_bytes(data[7:10], "little") + 1
            elif kind == b"VP8 " and not width:
                data = stream.read(10)
                if data[3:6] != b"\x9d\x01\x2a":
                    raise ValueError("WebP 크기를 읽지 못했습니다.")
                width, height = int.from_bytes(data[6:8], "little") & 16383, int.from_bytes(data[8:10], "little") & 16383
            elif kind == b"VP8L" and not width:
                data = stream.read(5)
                value = int.from_bytes(data[1:5], "little")
                width, height = (value & 16383) + 1, ((value >> 14) & 16383) + 1
            elif kind == b"ANMF":
                frames += 1
            elif kind in {b"eXIf", b"EXIF"}:
                if size > 1024 * 1024:
                    raise ValueError("이미지 메타데이터가 너무 큽니다.")
                orientation = _exif_orientation(stream.read(size))
            stream.seek(payload_start + size + (4 if png else size % 2))
            if kind == b"IEND":
                break
        return width, height, orientation, max(1, frames), "PNG" if png else "WEBP", resolution


def raster_header(source: Path, frame_index: int = 0) -> dict:
    """Read metadata without copy/convert/load and without a pixel-count cutoff."""
    chunks = _chunk_header(source)
    resolution = None
    if chunks:
        width, height, orientation, frames, image_format, resolution = chunks
        if frame_index >= frames or frame_index < 0:
            raise ValueError("이미지 프레임 범위를 확인해 주세요.")
        if image_format == "PNG" and frame_index:
            raise ValueError("큰 애니메이션 PNG는 프레임별 PNG 또는 TIFF로 저장해 주세요.")
    else:
      with _HEADER_LOCK:
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None
        try:
            with Image.open(source) as image:
                image.seek(int(frame_index))
                width, height = image.size
                orientation = int(Image.Image.getexif(image).get(274, 1))
                resolution = image.info.get("dpi")
                frames = int(getattr(image, "n_frames", 1))
                image_format = image.format
        finally:
            Image.MAX_IMAGE_PIXELS = previous
    if orientation not in range(1, 9):
        orientation = 1
    if orientation >= 5:
        width, height = height, width
    if width <= 0 or height <= 0:
        raise ValueError("이미지 크기를 읽을 수 없습니다.")
    dpi = width / (8.2677 if height >= width else 11.6929)
    if isinstance(resolution, (tuple, list)) and len(resolution) >= 2:
        candidate = resolution[1 if orientation >= 5 else 0]
        if isinstance(candidate, (float, int)) and math.isfinite(candidate) and 20 <= candidate <= 2400:
            dpi = float(candidate)
    return {"width": width, "height": height, "dpi": dpi, "orientation": orientation,
            "frames": frames, "format": image_format}


def tile_plan(width: int, height: int, max_pixels: int, overlap: int = 64) -> list[dict]:
    """Disjoint half-open cores cover the canvas; bounded tiles include context."""
    if min(width, height, max_pixels) < 1:
        raise ValueError("이미지 크기와 타일 화소 예산은 양수여야 합니다.")
    side = max(1, math.isqrt(max_pixels))
    margin = min(overlap, max(0, (side - 1) // 4))
    step = max(1, side - margin * 2)
    tiles = []
    for y0 in range(0, height, step):
        for x0 in range(0, width, step):
            x1, y1 = min(width, x0 + step), min(height, y0 + step)
            left, top = max(0, x0 - margin), max(0, y0 - margin)
            right, bottom = min(width, x1 + margin), min(height, y1 + margin)
            tiles.append({"x": left, "y": top, "width": right - left, "height": bottom - top,
                          "core": [x0, y0, x1, y1]})
    return tiles


def _cleanup_owned_workspace(work, folder, prefix, wait_seconds=2.0):
    """Retry short Windows sharing conflicts without following unknown paths."""
    work = Path(work)
    try:
        folder = Path(folder).resolve()
        if work.resolve().parent != folder or work.name != '.' + prefix:
            return False, 'unexpected_workspace_path'
    except OSError as exc:
        return False, f'{type(exc).__name__}: {exc}'
    deadline = time.monotonic() + wait_seconds
    attempt = 0
    while True:
        try:
            shutil.rmtree(work)
            return True, None
        except FileNotFoundError:
            return True, None
        except OSError as exc:
            sharing = isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in {5, 32, 33}
            remaining = deadline - time.monotonic()
            if not sharing or remaining <= 0:
                return False, f'{type(exc).__name__}: {exc}'
            time.sleep(min(.05 * 2 ** min(attempt, 3), .25, remaining))
            attempt += 1


def _write_lifecycle(path, state):
    # Local diagnostics must not replace the decoder result on their own I/O.
    try:
        Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        pass


def prepare_raster_tiles(source, folder, frame_index, max_pixels, budget_bytes) -> dict:
    from .process_ownership import ProcessJob
    source, folder = Path(source).resolve(), Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    header = raster_header(source, frame_index)
    max_pixels, budget_bytes = int(max_pixels), int(budget_bytes)
    if max_pixels < 1 or budget_bytes < 96 * 1024 * 1024:
        raise ValueError("큰 이미지 처리에 필요한 여유 메모리가 부족합니다.")
    # RGB spools, overlap PNGs and decoder scratch. This is a conservative disk
    # estimate, not a memory guarantee or an estimate of the compressed source.
    required = header["width"] * header["height"] * 16 + max_pixels * 4 + 64 * 1024 * 1024
    if shutil.disk_usage(folder).free < required:
        raise RuntimeError(f"큰 이미지의 임시 저장 공간이 부족합니다. 약 {math.ceil(required / 1024**3)}GB를 비워 주세요.")
    prefix = f"raster-{frame_index}-{uuid.uuid4().hex[:8]}"
    work = folder / ("." + prefix)
    work.mkdir()
    manifest, result_path, gate = work / "input.json", work / "result.json", work / "ready"
    payload = {"source": str(source), "folder": str(folder), "work": str(work), "prefix": prefix,
               "frame_index": int(frame_index), "max_pixels": max_pixels, "budget_bytes": budget_bytes,
               "header": header, "result_path": str(result_path), "gate": str(gate)}
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    command = ([sys.executable, "--image-worker", str(manifest)] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "converter.image_worker", str(manifest)])
    process, success, failure = None, None, None
    started = time.monotonic()
    lifecycle_path = folder / (prefix + '-lifecycle.json')
    lifecycle = {'phase': 'created', 'frame_index': int(frame_index),
                 'pixel_budget': max_pixels, 'commit_budget_bytes': budget_bytes}
    _write_lifecycle(lifecycle_path, lifecycle)
    try:
        with ProcessJob(budget_bytes=budget_bytes) as owner:
            with (work / "stderr.txt").open("wb") as error_stream:
                process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_stream,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                lifecycle.update(phase='child_created', child_pid=process.pid)
                _write_lifecycle(lifecycle_path, lifecycle)
                try:
                    owner.assign(process)
                except Exception:
                    process.terminate()
                    process.wait(timeout=10)
                    raise RuntimeError("큰 이미지 처리 프로세스를 안전하게 시작하지 못했습니다.") from None
                gate.write_text("owned", encoding="ascii")
                lifecycle['phase'] = 'owned_child_running'
                _write_lifecycle(lifecycle_path, lifecycle)
                try:
                    code = process.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("큰 이미지 준비 시간이 초과되었습니다. 이미지를 나누어 다시 시도해 주세요.") from None
                lifecycle.update(phase='child_exited', child_returncode=code, child_exit_confirmed=True)
                _write_lifecycle(lifecycle_path, lifecycle)
                if not result_path.is_file():
                    raise RuntimeError("이미지 디코더가 메모리 예산을 초과했거나 파일을 읽지 못했습니다. 예산을 확보하거나 파일을 나누어 주세요.")
                result = json.loads(result_path.read_text(encoding="utf-8"))
                lifecycle.update(phase='result_read', worker_decoder=result.get('decoder'),
                                 worker_diagnostic=result.get('diagnostic'), worker_error=result.get('error'))
                _write_lifecycle(lifecycle_path, lifecycle)
                if code or result.get("error"):
                    raise RuntimeError(result.get("error", "큰 이미지를 읽지 못했습니다."))
        for path in [result["image_path"]] + [tile["path"] for tile in result["tiles"]]:
            if Path(path).resolve().parent != folder or not Path(path).is_file():
                raise RuntimeError("큰 이미지 처리 결과의 경로가 올바르지 않습니다.")
        lifecycle.update(phase='outputs_validated', validated_png_assets=1 + len(result['tiles']))
        success = result
        return result
    except Exception as exc:
        failure = exc
        lifecycle.update(phase='decoder_failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        reap_error = None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                else:
                    process.wait(timeout=0)
                lifecycle.update(child_exit_confirmed=True, child_returncode=process.returncode)
            except Exception as exc:
                reap_error = exc
                lifecycle.update(child_exit_confirmed=False, reap_error=str(exc))
        try:
            lifecycle['worker_progress'] = json.loads((work / 'progress.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        # Cleanup cannot turn validated output into an error, or obscure a
        # decoder failure. Keep a locked scratch folder for later inspection.
        if reap_error is None:
            cleaned, cleanup_error = _cleanup_owned_workspace(work, folder, prefix)
        else:
            cleaned, cleanup_error = False, 'owned_child_not_reaped'
        lifecycle.update(cleanup_complete=cleaned, cleanup_error=cleanup_error,
                         elapsed_seconds=round(time.monotonic() - started, 3))
        _write_lifecycle(lifecycle_path, lifecycle)
        if success is not None and not cleaned:
            success.setdefault('warnings', []).append('임시 파일 일부를 정리하지 못했습니다. 변환 결과는 정상적으로 보관했습니다.')
        if reap_error is not None and failure is None:
            raise RuntimeError('큰 이미지 처리 프로세스의 종료를 확인하지 못했습니다. 앱을 종료한 뒤 다시 시도해 주세요.') from reap_error
