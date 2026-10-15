"""Editable native PDF text, tables, vector form shapes and local pictures.

This is text-layer extraction, not an OCR accuracy claim. Every native glyph is
kept editable, including letters inside diagrams. No document leaves this PC.
"""
from __future__ import annotations

import ctypes
import math
from pathlib import Path

from .runtime_bootstrap import configure_native_runtime
configure_native_runtime()
import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from .ocr import _find_tables, _assign_table_text, _find_text_frames
from .editable_graphics import native_pdf_shapes, extract_editable_graphics, apply_native_table_styles


def _glyphs(page, width: int, height: int):
    pdf_width, pdf_height = page.get_size()
    sx, sy = width / pdf_width, height / pdf_height
    textpage = page.get_textpage()
    glyphs = []
    try:
        for index in range(textpage.count_chars()):
            text = textpage.get_text_range(index, 1)
            if not text or text in "\r\n\x00":
                continue
            if text.isspace() and pdfium.raw.FPDFText_IsGenerated(textpage, index):
                continue
            left, bottom, right, top = textpage.get_charbox(index)
            ll, lb, lr, lt = textpage.get_charbox(index, loose=True)
            if right <= left or top <= bottom:
                continue
            origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
            pdfium.raw.FPDFText_GetCharOrigin(textpage, index, ctypes.byref(origin_x), ctypes.byref(origin_y))
            matrix = pdfium.raw.FS_MATRIX()
            matrix_ok = pdfium.raw.FPDFText_GetMatrix(textpage, index, ctypes.byref(matrix))
            scale = math.hypot(matrix.c, matrix.d) if matrix_ok else 0.0
            # The font size is in text-object units; its matrix can scale it.
            # Native PDFs commonly use a large nominal size with a small CTM.
            font_size = float(pdfium.raw.FPDFText_GetFontSize(textpage, index)) * scale * sy
            if not math.isfinite(font_size) or font_size <= 0:
                font_size = (lt - lb) * sy
            name_buffer, flags = ctypes.create_string_buffer(256), ctypes.c_int()
            pdfium.raw.FPDFText_GetFontInfo(textpage, index, name_buffer, len(name_buffer), ctypes.byref(flags))
            source_font = name_buffer.value.decode("utf-8", errors="replace")
            red, green, blue, alpha = [ctypes.c_uint() for _ in range(4)]
            has_color = pdfium.raw.FPDFText_GetFillColor(textpage, index, ctypes.byref(red), ctypes.byref(green), ctypes.byref(blue), ctypes.byref(alpha))
            color = f"#{red.value:02x}{green.value:02x}{blue.value:02x}" if has_color else "#000000"
            glyphs.append({"text": text, "x": max(0, ll * sx), "y": max(0, (pdf_height - lt) * sy),
                           "width": min(width, lr * sx) - max(0, ll * sx),
                           "height": min(height, (pdf_height - lb) * sy) - max(0, (pdf_height - lt) * sy),
                           "ink": [left * sx, (pdf_height - top) * sy, right * sx, (pdf_height - bottom) * sy],
                           "baseline": (pdf_height - origin_y.value) * sy,
                           "font_size": max(3, font_size), "index": index, "color": color,
                           "source_font": source_font, "bold": "bold" in source_font.lower() or bool(flags.value & 262144)})
    finally:
        textpage.close()
    return glyphs


def _group_lines(glyphs, include_baseline=False):
    lines = []
    for glyph in sorted(glyphs, key=lambda item: (item["baseline"], item["x"])):
        line = next((line for line in reversed(lines[-8:]) if abs(line["baseline"] - glyph["baseline"]) < max(1.5, glyph["font_size"] * .10)), None)
        if line is None:
            lines.append({"baseline": glyph["baseline"], "glyphs": [glyph]})
        else:
            line["glyphs"].append(glyph)
    blocks = []
    for line in lines:
        groups = []
        for glyph in sorted(line["glyphs"], key=lambda item: item["x"]):
            previous = groups[-1][-1] if groups else None
            gap = glyph["x"] - (previous["x"] + previous["width"]) if previous else 0
            explicit_space = previous is not None and previous["text"].isspace() and gap <= max(previous["font_size"], glyph["font_size"])
            if previous is None or (gap > max(previous["font_size"], glyph["font_size"]) * .45 and not explicit_space) or abs(previous["font_size"] - glyph["font_size"]) > max(previous["font_size"], glyph["font_size"]) * .3 or previous["color"] != glyph["color"] or previous["bold"] != glyph["bold"]:
                groups.append([glyph])
            else:
                groups[-1].append(glyph)
        for group in groups:
            fragments = []
            previous = None
            for item in group:
                if previous is not None and not previous["text"].isspace() and not item["text"].isspace() and item["x"] - previous["x"] - previous["width"] > item["font_size"] * .16:
                    fragments.append(" ")
                fragments.append(item["text"])
                previous = item
            text = "".join(fragments).strip()
            if not text:
                continue
            x, y = min(item["x"] for item in group), min(item["y"] for item in group)
            right = max(item["x"] + item["width"] for item in group)
            bottom = max(item["y"] + item["height"] for item in group)
            blocks.append({"type": "text", "x": x, "y": y, "width": right - x, "height": bottom - y,
                           "text": text, "font_size": float(np.median([item["font_size"] for item in group])),
                           "confidence": None, "source": "native-pdf-text", "native_glyphs": len(group),
                           "color": group[0]["color"], "bold": group[0]["bold"], "source_font": group[0]["source_font"]})
            if include_baseline:
                blocks[-1]["_baseline"] = line["baseline"]
    return blocks


def _cell_text(glyphs):
    """Keep a raised, adjoining footnote after its label in cell reading order.

    PDF superscripts have an earlier baseline than their label. Only small
    footnote symbols that touch the label's upper-right area are joined;
    ordinary lines and positioned text outside tables retain their geometry.
    """
    fragments = _group_lines(glyphs, include_baseline=True)
    rows = []
    for block in sorted(fragments, key=lambda item: (item["_baseline"], item["x"])):
        if rows and abs(block["_baseline"] - rows[-1]["baseline"]) < max(1.5, block["font_size"] * .1):
            rows[-1]["blocks"].append(block)
        else:
            rows.append({"baseline": block["_baseline"], "blocks": [block]})
    lines = []
    for row in rows:
        blocks = sorted(row["blocks"], key=lambda item: item["x"])
        line = dict(blocks[0])
        line["text"] = " ".join(block["text"] for block in blocks)
        line["x"] = min(block["x"] for block in blocks)
        line["y"] = min(block["y"] for block in blocks)
        line["width"] = max(block["x"] + block["width"] for block in blocks) - line["x"]
        line["height"] = max(block["y"] + block["height"] for block in blocks) - line["y"]
        lines.append(line)
    attached = set()
    symbols = set("*∗†‡⁎⁑⁕")
    for marker_index, marker in enumerate(lines):
        text = marker["text"]
        if not 1 <= len(text) <= 5 or any(char not in symbols for char in text):
            continue
        candidates = []
        for label_index, label in enumerate(lines):
            if label_index == marker_index or label_index in attached:
                continue
            if not any(char.isalnum() for char in label["text"]):
                continue
            height = label["height"]
            gap = marker["x"] - label["x"] - label["width"]
            raised = label["y"] - marker["y"]
            if (marker["height"] <= height * .85 and
                    -height * .1 <= gap <= height * .4 and
                    0 < raised <= height * .6 and
                    marker["y"] + marker["height"] > label["y"]):
                candidates.append((abs(gap) + raised, label_index))
        if candidates:
            _, label_index = min(candidates)
            lines[label_index]["text"] += text
            attached.add(marker_index)
    return "\n".join(line["text"] for index, line in
                     sorted(enumerate(lines), key=lambda item: (item[1]["y"], item[1]["x"]))
                     if index not in attached)


def recognize_native_pdf_page(page, image_path: Path, options: dict | None = None) -> dict | None:
    options = options or {}
    if options.get("force_ocr"):
        return None
    # Native coordinates currently assume an unrotated visible page origin.
    # Use the rendered OCR path for /Rotate or shifted CropBox/MediaBox pages
    # rather than placing correctly read text and form objects at wrong points.
    if page.get_rotation() != 0:
        return None
    left, bottom, _, _ = page.get_bbox()
    if abs(left) > 1e-6 or abs(bottom) > 1e-6:
        return None
    image_path = Path(image_path)
    with Image.open(image_path) as source:
        rgb = np.asarray(source.convert("RGB"))
    height, width = rgb.shape[:2]
    glyphs = _glyphs(page, width, height)
    nonspace = [glyph for glyph in glyphs if glyph["text"].strip()]
    if len(nonspace) < 5 or sum("�" in glyph["text"] for glyph in nonspace) > len(nonspace) * .02:
        return None
    image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    tables, _ = _find_tables(image) if options.get("detect_tables", True) else ([], None)
    if options.get('detect_tables', True):
        tables.extend(_find_text_frames(image, _group_lines(glyphs), tables))
    used = set()
    for table in tables:
        local_fonts = []
        for cell in table["cells"]:
            local_glyphs = [glyph for glyph in glyphs if glyph["index"] not in used and
                            cell["x"] <= glyph["x"] + glyph["width"] / 2 <= cell["x"] + cell["width"] and
                            cell["y"] <= glyph["y"] + glyph["height"] / 2 <= cell["y"] + cell["height"]]
            used.update(glyph["index"] for glyph in local_glyphs)
            cell["text"] = _cell_text(local_glyphs)
            cell["confidence"] = None
            cell.pop("_texts", None)
            cell.pop("_scores", None)
            table["rows"][cell["row"]][cell["col"]] = cell["text"]
            local_fonts.extend(glyph["font_size"] for glyph in local_glyphs)
        table["font_size"] = float(np.median(local_fonts)) if local_fonts else 14
    outside = _group_lines([glyph for glyph in glyphs if glyph["index"] not in used])
    for table in tables:
        table["source"] = "native-pdf-text"
    graphic_texts = []
    for glyph in nonspace:
        left, top, right, bottom = glyph["ink"]
        graphic_texts.append({"x": left, "y": top, "width": right - left, "height": bottom - top})
    shapes, protected = native_pdf_shapes(page, width, height, tables)
    blocks = extract_editable_graphics(image, graphic_texts, tables, image_path,
                                      native_shapes=shapes, protected_rects=protected)
    apply_native_table_styles(page, width, height, tables)
    for index, block in enumerate(blocks):
        block["id"] = f"{image_path.stem}-graphic-{index + 1}"
    for index, block in enumerate(sorted(outside + tables, key=lambda item: (item["y"], item["x"]))):
        block["id"] = f"{image_path.stem}-native-{index + 1}"
        blocks.append(block)
    return {"blocks": blocks, "engine": "native", "warnings": [
        "PDF에 포함된 원본 글자를 직접 추출했습니다. OCR 인식률을 측정한 결과가 아닙니다.",
        "글자·표·양식 테두리를 편집 가능한 객체로 분리하고 사진과 복잡한 그림은 해당 영역만 그림으로 보존했습니다. 원본과 대조해 주세요."],
        "native_characters": len(nonspace), "measured": False, "source": "native-pdf-text-layer"}
