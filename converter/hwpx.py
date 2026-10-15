"""Portable, spatial HWPX and editable HTML exports.

HWPX uses ordinary body paragraphs, actual table cells and embedded PNGs.
The empty package is airmang/python-hwpx's Apache-2.0 Skeleton.hwpx; its license
is distributed beside this module. Drawing child order follows that project's
corpus-tested ``oxml/objects.py`` implementation rather than XSD inheritance
order, which differs from documents written by Hancom. Reference sources:
https://github.com/airmang/python-hwpx
https://github.com/hancom-io/hwpx-owpml-model
https://tech.hancom.com/hwpxformat/

본 제품은 한컴의 HWP 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
Package/XML verification is not a claim of visual equivalence or Hancom opening.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
from functools import lru_cache
from io import BytesIO
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from PIL import Image, ImageFont

NS = {
    "ha": "http://www.hancom.co.kr/hwpml/2011/app",
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hp10": "http://www.hancom.co.kr/hwpml/2016/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "hhs": "http://www.hancom.co.kr/hwpml/2011/history",
    "hm": "http://www.hancom.co.kr/hwpml/2011/master-page",
    "hpf": "http://www.hancom.co.kr/schema/2011/hpf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "opf": "http://www.idpf.org/2007/opf/",
    "ooxmlchart": "http://www.hancom.co.kr/hwpml/2016/ooxmlchart",
    "hwpunitchar": "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar",
    "epub": "http://www.idpf.org/2007/ops",
    "config": "urn:oasis:names:tc:opendocument:xmlns:config:1.0",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "pkg": "http://www.hancom.co.kr/hwpml/2016/meta/pkg#",
    "odf": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_MARGIN = {"left": "0", "right": "0", "top": "0", "bottom": "0"}
_MATRIX = {"e1": "1", "e2": "0", "e3": "0", "e4": "0", "e5": "1", "e6": "0"}
MAX_EMBEDDED_IMAGE_BYTES = 256 * 1024 * 1024


def _image_budget(total: int, payload: bytes) -> int:
    """Bound accumulated PNGs before retaining or base64-encoding the next one."""
    total += len(payload)
    if total > MAX_EMBEDDED_IMAGE_BYTES:
        raise ValueError("문서의 그림 용량이 너무 큽니다(최대 256MB). 변환할 쪽을 나눠 다시 저장해 주세요.")
    return total


def _tag(name: str) -> str:
    prefix, local = name.split(":", 1)
    return "{" + NS[prefix] + "}" + local


def _child(parent, tag_name, **attrs):
    return ET.SubElement(parent, _tag(tag_name), {k: str(v) for k, v in attrs.items()})


def _xml(element) -> bytes:
    payload = ET.tostring(element, encoding="utf-8")
    if element.tag in (_tag("hh:head"), _tag("hs:sec")):
        # Hancom expects its complete compatibility namespace set on these
        # roots, including unused prefixes; ElementTree drops unused names.
        opening = payload.split(b">", 1)[0]
        declarations = b"".join(
            f' xmlns:{prefix}="{uri}"'.encode("ascii")
            for prefix, uri in NS.items()
            if prefix not in ("rdf", "pkg", "odf") and f"xmlns:{prefix}=".encode() not in opening
        )
        root_end = payload.find(b" ")
        if root_end < 0 or root_end > payload.find(b">"):
            root_end = payload.find(b">")
        payload = payload[:root_end] + declarations + payload[root_end:]
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + payload


def _text(value) -> str:
    return _ILLEGAL_XML.sub("", str(value if value is not None else "")).replace("\r\n", "\n").replace("\r", "\n")


def _text_content(element, value):
    """HWPX tabs/line breaks belong inside hp:t, not sibling runs."""
    last = None
    for piece in re.split(r"([\t\n])", _text(value)):
        if piece in ("\t", "\n"):
            last = _child(element, "hp:tab" if piece == "\t" else "hp:lineBreak")
        elif last is None:
            element.text = (element.text or "") + piece
        else:
            last.tail = (last.tail or "") + piece


def _number(value, default=0.0) -> float:
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return value if math.isfinite(value) else default


def _color(value, default="#000000"):
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else default


@lru_cache(maxsize=96)
def _body_font(size, bold):
    # Use the font already installed on Windows; never redistribute OS fonts.
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / ("malgunbd.ttf" if bold else "malgun.ttf")
    try:
        return ImageFont.truetype(str(path), max(1, round(size * 4)))
    except OSError:
        return None


def _body_width_ratio(block, box):
    if block.get("source") != "native-pdf-text" or block.get("text_edited"):
        return 100
    text = _text(block.get("text", ""))
    if not text or "\n" in text or "\t" in text:
        return 100
    font = _body_font(_number(block.get("font_size"), 20), bool(block.get("bold")))
    if font is None:
        return 100
    measured = font.getlength(text) / 4
    # Native glyph bounds used the source font. Keep its physical font height
    # and fit only horizontal glyph width when the installed font is wider.
    return max(50, min(100, math.floor(100 * max(1, box[2] - 1) / max(1, measured))))


def _page_geometry(page):
    width = _number(page.get("width"))
    height = _number(page.get("height"))
    if not 0 < width <= 1000000 or not 0 < height <= 1000000:
        raise ValueError("페이지 크기가 올바르지 않습니다.")
    dpi = _number(page.get("dpi"), 150)
    if not 36 <= dpi <= 1000000:
        dpi = 150
    return width, height, 7200 / dpi


def _box(block, page):
    pw, ph, _ = _page_geometry(page)
    x, y = _number(block.get("x")), _number(block.get("y"))
    w, h = _number(block.get("width")), _number(block.get("height"))
    left, top = min(pw, max(0, x)), min(ph, max(0, y))
    right, bottom = min(pw, max(0, x + w)), min(ph, max(0, y + h))
    if block.get("type") == "shape":
        # A real horizontal/vertical line has zero area; keep its endpoints.
        visible_x = right > left or (w == 0 and 0 <= x <= pw)
        visible_y = bottom > top or (h == 0 and 0 <= y <= ph)
        if w >= 0 and h >= 0 and (w > 0 or h > 0) and visible_x and visible_y:
            return left, top, max(.01, right - left), max(.01, bottom - top)
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _picture_overlaps_text(box, page):
    """A transparent picture still captures editor clicks across its full box.

    Put overlapping pictures behind editable body text and real table objects;
    alpha holes and a low zOrder alone do not change Hancom's hit testing.
    """
    x, y, width, height = box
    for block in page.get("blocks", []):
        if block.get("type") not in {"text", "table"}:
            continue
        if block.get("type") == "text" and not _text(block.get("text", "")).strip():
            continue
        other = _box(block, page)
        if other is None:
            continue
        bx, by, bw, bh = other
        if min(x + width, bx + bw) > max(x, bx) and min(y + height, by + bh) > max(y, by):
            return True
    return False


def _clip_segment(a, b, width, height):
    x, y = a
    dx, dy = b[0] - x, b[1] - y
    low, high = 0., 1.
    for p, q in ((-dx, x), (dx, width - x), (-dy, y), (dy, height - y)):
        if abs(p) < 1e-12:
            if q < 0:
                return None
        else:
            edge = q / p
            if p < 0:
                low = max(low, edge)
            else:
                high = min(high, edge)
            if low > high:
                return None
    return ((x + low * dx, y + low * dy), (x + high * dx, y + high * dy))


def _shape_geometry(block, box):
    """Clip source points together with their box, never shift a cropped rule."""
    kind = block.get("shape", "rect")
    bw, bh = _number(block.get("width")), _number(block.get("height"))
    default = [[0, 0], [bw, bh]] if kind == "line" else [[0, 0], [bw, 0], [bw, bh], [0, bh]]
    points = block.get("points") or default
    if not isinstance(points, list) or not 2 <= len(points) <= 10000 or any(not isinstance(p, (list, tuple)) or len(p) != 2 for p in points):
        raise ValueError("도형 좌표가 올바르지 않습니다.")
    dx, dy = _number(block.get("x")) - box[0], _number(block.get("y")) - box[1]
    points = [(_number(px) + dx, _number(py) + dy) for px, py in points]
    closed = kind == "rect" or bool(block.get("closed", False))
    if not closed:
        segments = [segment for a, b in zip(points, points[1:])
                    if (segment := _clip_segment(a, b, box[2], box[3])) is not None]
        return points, segments, False
    for axis, edge, keep_greater in ((0, 0, True), (0, box[2], False), (1, 0, True), (1, box[3], False)):
        clipped = []
        if not points:
            break
        previous = points[-1]
        inside = lambda p: p[axis] >= edge if keep_greater else p[axis] <= edge
        for current in points:
            if inside(current) != inside(previous):
                fraction = (edge - previous[axis]) / (current[axis] - previous[axis])
                clipped.append(tuple(previous[i] + fraction * (current[i] - previous[i]) for i in (0, 1)))
            if inside(current):
                clipped.append(current)
            previous = current
        points = clipped
    return points, [], True


def _asset_path(url: str, asset_root: Path, document_id=None) -> Path:
    """Resolve only a same-origin, two-segment job asset, including symlink checks."""
    parsed = urlsplit(str(url))
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("외부 이미지 주소는 내보내기에 사용할 수 없습니다.")
    parts = unquote(parsed.path).split("/")
    if len(parts) != 5 or parts[:3] != ["", "api", "assets"]:
        raise ValueError("이미지 주소 형식이 올바르지 않습니다.")
    job, filename = parts[3:]
    if not all(_SAFE_SEGMENT.fullmatch(p) for p in (job, filename)) or ".." in (job, filename):
        raise ValueError("이미지 파일 경로가 올바르지 않습니다.")
    if document_id and job != str(document_id):
        raise ValueError("현재 문서에 속하지 않는 이미지입니다.")
    root = Path(asset_root).resolve()
    path = (root / job / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("이미지 파일을 찾을 수 없습니다.")
    return path


def _image_bytes(block, page, asset_root, document_id):
    path = _asset_path(block.get("image_url", ""), asset_root, document_id)
    with Image.open(path) as original:
        image = original.convert("RGBA" if "A" in original.getbands() else "RGB")
        # Crops supplied by OCR are complete images; clip out-of-page geometry
        # proportionally rather than compressing the full image into a smaller box.
        bbox = _box(block, page)
        x, y = _number(block.get("x")), _number(block.get("y"))
        w, h = _number(block.get("width")), _number(block.get("height"))
        if bbox and w > 0 and h > 0:
            bx, by, bw, bh = bbox
            crop = (
                round((bx - x) / w * image.width), round((by - y) / h * image.height),
                round((bx + bw - x) / w * image.width), round((by + bh - y) / h * image.height),
            )
            if crop != (0, 0, image.width, image.height):
                image = image.crop(crop)
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def _table_grid(block, box):
    """Return authoritative edited rows, spans and source-informed grid boundaries."""
    source_rows = block.get("rows", [])
    if not isinstance(source_rows, list):
        raise ValueError("표 데이터가 올바르지 않습니다.")
    rows = [[_text(cell) for cell in row] for row in source_rows if isinstance(row, list)]
    if not rows or not max(map(len, rows), default=0):
        rows = [[""]]
    nr, nc = len(rows), max(map(len, rows))
    if nr * nc > 50000:
        raise ValueError("표가 너무 큽니다.")
    rows = [r + [""] * (nc - len(r)) for r in rows]
    x, y, width, height = box
    xs, ys = {0: 0.0, nc: width}, {0: 0.0, nr: height}
    for key, points, count, total in (("column_widths", xs, nc, width), ("row_heights", ys, nr, height)):
        dimensions = block.get(key)
        if isinstance(dimensions, list) and len(dimensions) == count:
            values = [_number(v) for v in dimensions]
            if all(v > 0 for v in values):
                running = 0.0
                for i, value in enumerate(values, 1):
                    running += value
                    points[i] = total * running / sum(values)
    spans, occupied = {}, set()
    cells = block.get("cells", [])
    if not isinstance(cells, list):
        cells = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        r, c = int(_number(cell.get("row"), -1)), int(_number(cell.get("col"), -1))
        if not (0 <= r < nr and 0 <= c < nc) or (r, c) in occupied:
            continue
        rs = max(1, min(nr - r, int(_number(cell.get("rowspan"), 1))))
        cs = max(1, min(nc - c, int(_number(cell.get("colspan"), 1))))
        covered = {(rr, cc) for rr in range(r, r + rs) for cc in range(c, c + cs)}
        if covered & occupied:
            continue
        spans[(r, c)] = (rs, cs)
        occupied.update(covered)
        if all(key in cell for key in ("x", "width")):
            if c:
                xs[c] = max(0, min(width, _number(cell["x"]) - x))
            if c + cs < nc:
                xs[c + cs] = max(0, min(width, _number(cell["x"]) + _number(cell["width"]) - x))
        if all(key in cell for key in ("y", "height")):
            if r:
                ys[r] = max(0, min(height, _number(cell["y"]) - y))
            if r + rs < nr:
                ys[r + rs] = max(0, min(height, _number(cell["y"]) + _number(cell["height"]) - y))
    def boundaries(points, count, size):
        keys = sorted(points)
        if any(points[b] <= points[a] for a, b in zip(keys, keys[1:])):
            return [size * i / count for i in range(count + 1)]
        values = [0.0] * (count + 1)
        for a, b in zip(keys, keys[1:]):
            for i in range(a, b + 1):
                values[i] = points[a] + (points[b] - points[a]) * (i - a) / (b - a)
        return values
    covered = set()
    for (r, c), (rs, cs) in spans.items():
        covered.update((rr, cc) for rr in range(r, r + rs) for cc in range(c, c + cs) if (rr, cc) != (r, c))
    return rows, spans, covered, boundaries(xs, nc, width), boundaries(ys, nr, height)


def _body_lines(page):
    """Join same-line font fragments into body paragraphs, never drawing boxes.

    OCR/native PDF fragments can split a sentence at a font change. Retain each
    fragment and its style while putting one visual line in one body paragraph.
    Explicit edited newlines become separate, freely editable paragraphs.
    """
    fragments = []
    for source_index, block in enumerate(page.get("blocks", [])):
        if block.get("type") != "text":
            continue
        box = _box(block, page)
        if box is None:
            continue
        pieces = _text(block.get("text", "")).split("\n")
        x, y, w, h = box
        for index, piece in enumerate(pieces):
            fragments.append((dict(block, text=piece, _body_source=source_index, _body_line=index),
                              (x, y + h * index / len(pieces), w, h / len(pieces))))
    lines = []
    for block, box in sorted(fragments, key=lambda item: (item[1][1], item[1][0])):
        x, y, w, h = box
        middle = y + h / 2
        matching = None
        for line in reversed(lines):
            lx, ly, lw, lh = line["box"]
            if y > ly + lh:
                break
            # Explicit user line breaks are semantic paragraph boundaries even
            # if twenty edited lines still share the original small OCR box.
            if any(other["_body_source"] == block["_body_source"] and
                   other["_body_line"] != block["_body_line"] for other, _ in line["fragments"]):
                continue
            centers_align = abs(middle - (ly + lh / 2)) <= max(2, min(h, lh) * 0.35)
            bottoms_align = abs((y + h) - (ly + lh)) <= max(2, min(h, lh) * 0.2)
            if centers_align or bottoms_align:
                matching = line
                break
        if matching is None:
            lines.append({"box": box, "fragments": [(block, box)]})
        else:
            lx, ly, lw, lh = matching["box"]
            right, bottom = max(lx + lw, x + w), max(ly + lh, y + h)
            matching["box"] = (min(lx, x), min(ly, y), right - min(lx, x), bottom - min(ly, y))
            matching["fragments"].append((block, box))
    for line in lines:
        line["fragments"].sort(key=lambda item: item[1][0])
    return sorted(lines, key=lambda line: (line["box"][1], line["box"][0]))


class _Builder:
    def __init__(self, header):
        self.header = header
        self.counter = 1000
        self.char_ids = {}
        self.chars = header.find("hh:refList/hh:charProperties", NS)
        self.base_char = deepcopy(self.chars[0])
        # The synthetic upstream fixture's names contain replacement characters.
        # Use a widely available Korean font instead of copying those names.
        for font in header.findall(".//hh:font", NS):
            font.set("face", "맑은 고딕")
        for style in header.findall(".//hh:style", NS):
            style.set("name", style.get("engName", "Normal"))
        para = header.find("hh:refList/hh:paraProperties/hh:paraPr", NS)
        para.find("hh:align", NS).set("horizontal", "LEFT")
        para.set("snapToGrid", "0")
        for spacing in para.findall(".//hh:lineSpacing", NS):
            spacing.set("value", "100")
        self.paras = header.find("hh:refList/hh:paraProperties", NS)
        self.base_para = deepcopy(para)
        self.para_ids = {}
        self.tabs = header.find("hh:refList/hh:tabProperties", NS)
        self.tab_ids = {}
        self.borders = header.find("hh:refList/hh:borderFills", NS)
        self.base_border = deepcopy(self.borders[0])
        self.border_ids = {}
        border = deepcopy(self.base_border)
        border.set("id", "3")
        for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
            border.find("hh:" + side, NS).set("type", "SOLID")
        self.borders.append(border)
        self.borders.set("itemCnt", str(len(self.borders)))

    def id(self):
        self.counter += 1
        return str(self.counter)

    def char(self, block, scale, width_ratio=100):
        size = max(100, min(10000, round(_number(block.get("font_size"), 20) * scale)))
        bold = bool(block.get("bold", False))
        color = str(block.get("color", "#000000"))
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            color = "#000000"
        key = (size, bold, color, width_ratio)
        if key not in self.char_ids:
            prop = deepcopy(self.base_char)
            ident = str(len(self.chars))
            prop.set("id", ident)
            prop.set("height", str(size))
            prop.set("textColor", color)
            ratio = prop.find("hh:ratio", NS)
            ratio.attrib.update({key: str(width_ratio) for key in ratio.attrib})
            prop.find("hh:fontRef", NS).attrib.update({k: "0" for k in prop.find("hh:fontRef", NS).attrib})
            if bold:
                _child(prop, "hh:bold")
            self.chars.append(prop)
            self.chars.set("itemCnt", str(len(self.chars)))
            self.char_ids[key] = ident
        return self.char_ids[key]

    def paragraph(self, parent, text, char_id="0", para_id="0"):
        p = _child(parent, "hp:p", id=self.id(), paraPrIDRef=para_id, styleIDRef="0", pageBreak="0", columnBreak="0", merged="0")
        run = _child(p, "hp:run", charPrIDRef=char_id)
        t = _child(run, "hp:t")
        _text_content(t, text)
        return p

    def body_style(self, left, before, height, tab_stops=()):
        key = (left, before, height, tuple(tab_stops))
        if key not in self.para_ids:
            prop = deepcopy(self.base_para)
            ident = str(len(self.paras))
            prop.set("id", ident)
            if tab_stops:
                if tuple(tab_stops) not in self.tab_ids:
                    tab_id = str(len(self.tabs))
                    tabs = _child(self.tabs, "hh:tabPr", id=tab_id, autoTabLeft="0", autoTabRight="0")
                    for position in tab_stops:
                        _child(tabs, "hh:tabItem", pos=position, type="LEFT", leader="NONE")
                    self.tabs.set("itemCnt", str(len(self.tabs)))
                    self.tab_ids[tuple(tab_stops)] = tab_id
                prop.set("tabPrIDRef", self.tab_ids[tuple(tab_stops)])
            for margin in prop.findall(".//hh:margin", NS):
                for name, value in (("intent", 0), ("left", left), ("right", 0), ("prev", before), ("next", 0)):
                    margin.find("hc:" + name, NS).set("value", str(value))
            for spacing in prop.findall(".//hh:lineSpacing", NS):
                spacing.attrib.update(type="FIXED", value=str(height), unit="HWPUNIT")
            self.paras.append(prop)
            self.paras.set("itemCnt", str(len(self.paras)))
            self.para_ids[key] = ident
        return self.para_ids[key]

    def body_text(self, section, line, scale, page_width, cursor):
        x, y, w, h = line["box"]
        left, top = round(x * scale), round(y * scale)
        font_height = max(int(self.chars[int(self.char(block, scale))].get("height"))
                          for block, _ in line["fragments"])
        height = max(1, round(h * scale), font_height)
        tab_stops = []
        previous_right = None
        fragments = []
        for block, box in line["fragments"]:
            gap = 0 if previous_right is None else max(0, box[0] - previous_right)
            font = _number(block.get("font_size"), 20)
            separator = "" if gap <= max(1, font * .15) else " "
            if gap > max(6, font):
                # Hancom tab stops are measured from the page's text origin,
                # independently of this paragraph's left indentation.
                tab_stops.append(round(box[0] * scale))
                separator = "\t"
            fragments.append((block, separator, _body_width_ratio(block, box)))
            previous_right = box[0] + box[2]
        para_id = self.body_style(left, max(0, top - cursor), height, tab_stops)
        p = _child(section, "hp:p", id=self.id(), paraPrIDRef=para_id, styleIDRef="0", pageBreak="0", columnBreak="0", merged="0")
        for block, separator, width_ratio in fragments:
            run = _child(p, "hp:run", charPrIDRef=self.char(block, scale, width_ratio))
            t = _child(run, "hp:t")
            _text_content(t, separator + _text(block.get("text", "")))
        # Initial layout cache restores the scanned line position. The paragraph
        # margins/spacing above also support normal text reflow after editing.
        segments = _child(p, "hp:linesegarray")
        _child(segments, "hp:lineseg", textpos="0", vertpos=top, vertsize=height,
               textheight=font_height, baseline=round(font_height * .85), spacing="0",
               horzpos=left, horzsize=max(1, round(page_width * scale) - left), flags="393216")
        return max(cursor, top) + height

    def sublist(self, parent, text, char_id, width, height):
        sub = _child(parent, "hp:subList", id=self.id(), textDirection="HORIZONTAL", lineWrap="BREAK", vertAlign="TOP", linkListIDRef="0", linkListNextIDRef="0", textWidth=width, textHeight=height, hasTextRef="0", hasNumRef="0")
        self.paragraph(sub, text, char_id)
        return sub

    def floating_position(self, shape, box, scale):
        x, y, w, h = [round(v * scale) for v in box]
        _child(shape, "hp:sz", width=max(1, w), height=max(1, h), widthRelTo="ABSOLUTE", heightRelTo="ABSOLUTE", protect="0")
        _child(shape, "hp:pos", treatAsChar="0", affectLSpacing="0", flowWithText="0", allowOverlap="1", holdAnchorAndSO="0", vertRelTo="PAPER", horzRelTo="PAPER", vertAlign="TOP", horzAlign="LEFT", vertOffset=y, horzOffset=x)
        ET.SubElement(shape, _tag("hp:outMargin"), dict(_MARGIN))

    def drawing(self, run, kind, box, scale, zorder):
        ident = self.id()
        shape = _child(run, "hp:" + kind, id=ident, zOrder=zorder, numberingType="PICTURE" if kind == "pic" else "NONE", textWrap="IN_FRONT_OF_TEXT", textFlow="BOTH_SIDES", lock="0", dropcapstyle="None", href="", groupLevel="0", instid=ident)
        w, h = max(1, round(box[2] * scale)), max(1, round(box[3] * scale))
        _child(shape, "hp:offset", x="0", y="0")
        _child(shape, "hp:orgSz", width=w, height=h)
        _child(shape, "hp:curSz", width=w, height=h)
        _child(shape, "hp:flip", horizontal="0", vertical="0")
        _child(shape, "hp:rotationInfo", angle="0", centerX=w // 2, centerY=h // 2, rotateimage="1")
        render = _child(shape, "hp:renderingInfo")
        for name in ("transMatrix", "scaMatrix", "rotMatrix"):
            ET.SubElement(render, _tag("hc:" + name), dict(_MATRIX))
        return shape, w, h

    def picture(self, run, box, scale, binary_id, zorder, background=False):
        shape, w, h = self.drawing(run, "pic", box, scale, zorder)
        if background:
            shape.set("textWrap", "BEHIND_TEXT")
        shape.set("reverse", "0")
        _child(shape, "hp:lineShape", color="#000000", width="0", style="NONE", endCap="FLAT",
               headStyle="NORMAL", tailStyle="NORMAL", headfill="0", tailfill="0",
               headSz="SMALL_SMALL", tailSz="SMALL_SMALL", outlineStyle="NORMAL", alpha="0")
        rect = _child(shape, "hp:imgRect")
        for i, (px, py) in enumerate(((0, 0), (w, 0), (w, h), (0, h))):
            _child(rect, "hc:pt" + str(i), x=px, y=py)
        _child(shape, "hp:imgClip", left="0", right=w, top="0", bottom=h)
        ET.SubElement(shape, _tag("hp:inMargin"), dict(_MARGIN))
        _child(shape, "hp:imgDim", dimwidth=w, dimheight=h)
        _child(shape, "hc:img", binaryItemIDRef=binary_id, bright="0", contrast="0", effect="REAL_PIC", alpha="0")
        _child(shape, "hp:effects")
        self.floating_position(shape, box, scale)

    def shape(self, run, block, box, scale, zorder):
        kind = block.get("shape", "rect")
        if kind not in {"line", "rect", "polyline"}:
            raise ValueError("지원하지 않는 도형입니다.")
        points, segments, closed = _shape_geometry(block, box)
        if (closed and not points) or (not closed and not segments):
            return
        fill = _color(block.get("fill_color"), None)
        if kind == "rect" and (box[0] != _number(block.get("x")) or box[1] != _number(block.get("y")) or
                               box[2] != _number(block.get("width")) or box[3] != _number(block.get("height"))):
            kind = "polyline"
        tag = "line" if kind == "line" else "rect" if kind == "rect" else "polygon" if closed else "curve"
        shape, w, h = self.drawing(run, tag, box, scale, zorder)
        # The form remains a genuine selectable drawing object. Its label is
        # ordinary body text above it, including labels on coloured rectangles.
        shape.set("textWrap", "BEHIND_TEXT")
        if kind == "rect":
            shape.set("ratio", "0")
        elif kind == "line":
            shape.set("isReverseHV", "0")
        stroke_style = block.get("stroke_style", "SOLID")
        if stroke_style not in {"SOLID", "DOT", "DASH"}:
            stroke_style = "SOLID"
        _child(shape, "hp:lineShape", color=_color(block.get("stroke_color")),
               width=max(1, round(max(.01, _number(block.get("stroke_width"), 1)) * scale)),
               style=stroke_style if block.get("stroke_color", "#000000") is not None and _number(block.get("stroke_width"), 1) > 0 else "NONE",
               endCap="FLAT", headStyle="NORMAL", tailStyle="NORMAL", headfill="0", tailfill="0",
               headSz="SMALL_SMALL", tailSz="SMALL_SMALL", outlineStyle="NORMAL", alpha="0")
        if fill and kind != "line":
            brush = _child(shape, "hc:fillBrush")
            _child(brush, "hc:winBrush", faceColor=fill, hatchColor="#000000", alpha="0")
        _child(shape, "hp:shadow", type="NONE", color="#B2B2B2", offsetX="0", offsetY="0", alpha="0")
        if kind == "rect":
            for index, (px, py) in enumerate(((0, 0), (w, 0), (w, h), (0, h))):
                _child(shape, "hc:pt" + str(index), x=px, y=py)
        else:
            local = [(round(px * scale), round(py * scale)) for px, py in points]
            if kind == "line":
                for name, (px, py) in zip(("startPt", "endPt"), segments[0]):
                    px, py = round(px * scale), round(py * scale)
                    _child(shape, "hc:" + name, x=px, y=py)
            elif closed:
                for px, py in local:
                    _child(shape, "hc:pt", x=px, y=py)
            else:
                for a, b in segments:
                    _child(shape, "hp:seg", type="LINE", x1=round(a[0] * scale), y1=round(a[1] * scale),
                           x2=round(b[0] * scale), y2=round(b[1] * scale))
        self.floating_position(shape, box, scale)

    def border_fill(self, style, scale):
        edges = []
        borders = style.get("borders") if isinstance(style.get("borders"), dict) else {}
        for side in ("left", "right", "top", "bottom"):
            edge = borders.get(side) if isinstance(borders.get(side), dict) else {}
            color = _color(edge.get("color", style.get("border_color")))
            px = _number(edge.get("width", style.get("border_width")), .1 * 7200 / (scale * 25.4))
            visible = edge.get("color", style.get("border_color", "#000000")) is not None and px > 0
            width = max(.1, min(5, px * scale * 25.4 / 7200))
            # HWPX table-border thickness has discrete millimetre values.
            width = min((.1, .12, .15, .2, .25, .3, .4, .5, .6, .7, 1, 1.5, 2, 3, 4, 5), key=lambda v: abs(v - width))
            line_style = edge.get('style', style.get('border_style', 'SOLID'))
            if line_style not in {'SOLID', 'DOT', 'DASH'}:
                line_style = 'SOLID'
            edges.append((color, width, visible, line_style))
        fill = _color(style.get("fill_color"), None)
        key = (tuple(edges), fill)
        if key not in self.border_ids:
            border = deepcopy(self.base_border)
            ident = str(max(int(b.get("id")) for b in self.borders) + 1)
            border.set("id", ident)
            for side, (color, width, visible, line_style) in zip(("leftBorder", "rightBorder", "topBorder", "bottomBorder"), edges):
                border.find("hh:" + side, NS).attrib.update(type=line_style if visible else "NONE", color=color, width=f"{width:g} mm")
            if fill:
                brush = _child(border, "hc:fillBrush")
                _child(brush, "hc:winBrush", faceColor=fill, hatchColor="#000000", alpha="0")
            self.borders.append(border)
            self.borders.set("itemCnt", str(len(self.borders)))
            self.border_ids[key] = ident
        return self.border_ids[key]

    def table(self, run, block, box, scale, zorder):
        rows, spans, covered, xs, ys = _table_grid(block, box)
        border_id = self.border_fill(block, scale)
        table = _child(run, "hp:tbl", id=self.id(), zOrder=zorder, numberingType="TABLE", textWrap="IN_FRONT_OF_TEXT", textFlow="BOTH_SIDES", lock="0", dropcapstyle="None", pageBreak="NONE", repeatHeader="0", rowCnt=len(rows), colCnt=len(rows[0]), cellSpacing="0", borderFillIDRef=border_id, noAdjust="1")
        self.floating_position(table, box, scale)
        ET.SubElement(table, _tag("hp:inMargin"), dict(_MARGIN))
        char_id = self.char(block, scale)
        cells = block.get("cells", [])
        styles = {(int(_number(c.get("row"), -1)), int(_number(c.get("col"), -1))): c
                  for c in (cells if isinstance(cells, list) else []) if isinstance(c, dict)}
        for r, values in enumerate(rows):
            tr = _child(table, "hp:tr")
            for c, text in enumerate(values):
                if (r, c) in covered:
                    continue
                rs, cs = spans.get((r, c), (1, 1))
                w = max(1, round(xs[c + cs] * scale) - round(xs[c] * scale))
                h = max(1, round(ys[r + rs] * scale) - round(ys[r] * scale))
                style = {**block, **styles.get((r, c), {})}
                tc = _child(tr, "hp:tc", name="", header="0", hasMargin="1", protect="0", editable="1", dirty="0", borderFillIDRef=self.border_fill(style, scale))
                sub = self.sublist(tc, text, char_id, w, h)
                sub.set("vertAlign", "CENTER")
                _child(tc, "hp:cellAddr", colAddr=c, rowAddr=r)
                _child(tc, "hp:cellSpan", colSpan=cs, rowSpan=rs)
                _child(tc, "hp:cellSz", width=w, height=h)
                _child(tc, "hp:cellMargin", left="70", right="70", top="35", bottom="35")


def _write_hwpx(document: dict, asset_root: Path, archive: ZipFile, *, bounded_images: bool):
    """Write each page/image once; only document-wide XML references accumulate."""
    pages = document.get("pages", [])
    from collections.abc import Sequence
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)) or not pages or len(pages) > 500:
        raise ValueError("내보낼 페이지가 없습니다.")
    with ZipFile(Path(__file__).with_name("Skeleton.hwpx")) as template:
        parts = {name: template.read(name) for name in template.namelist()}
    archive.writestr("mimetype", b"application/hwp+zip", compress_type=ZIP_STORED)
    header = ET.fromstring(parts["Contents/header.xml"])
    header.set("secCnt", str(len(pages)))
    builder = _Builder(header)
    base_section = ET.fromstring(parts.pop("Contents/section0.xml"))
    sec_pr_template = base_section.find(".//hp:secPr", NS)
    col_pr_template = base_section.find(".//hp:colPr", NS)
    package = ET.fromstring(parts["Contents/content.hpf"])
    metadata = package.find("opf:metadata", NS)
    metadata.clear()
    _child(metadata, "opf:title").text = _text(document.get("name", "변환 문서"))
    _child(metadata, "opf:language").text = "ko"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for key, val in (("creator", "PDF2HWPX"), ("lastsaveby", "PDF2HWPX"), ("CreatedDate", now), ("ModifiedDate", now)):
        _child(metadata, "opf:meta", name=key, content="text").text = val
    manifest, spine = package.find("opf:manifest", NS), package.find("opf:spine", NS)
    manifest.clear()
    spine.clear()
    _child(manifest, "opf:item", id="header", href="Contents/header.xml", **{"media-type": "application/xml"})
    _child(manifest, "opf:item", id="settings", href="settings.xml", **{"media-type": "application/xml"})
    _child(manifest, "opf:item", id="version", href="version.xml", **{"media-type": "application/xml"})
    _child(spine, "opf:itemref", idref="header", linear="yes")
    preview = []
    picture_counter = 0
    image_bytes_total = 0
    section_names = []
    for page_index, page in enumerate(pages):
        if any(block.get("role") == "graphics-background" for block in page.get("blocks", [])):
            from .editable_graphics import upgrade_legacy_page
            page = upgrade_legacy_page(page, asset_root, document.get("id"))
        pw, ph, scale = _page_geometry(page)
        section = ET.Element(_tag("hs:sec"))
        anchor_char = builder.char({"font_size": 100 / scale}, scale)
        anchor = builder.paragraph(section, "", anchor_char, builder.body_style(0, 0, 100))
        anchor.set("pageBreak", "1" if page_index else "0")
        anchor_run = anchor.find("hp:run", NS)
        sec_pr = deepcopy(sec_pr_template)
        sec_pr.set("id", str(page_index))
        page_pr = sec_pr.find("hp:pagePr", NS)
        page_pr.set("width", str(round(pw * scale)))
        page_pr.set("height", str(round(ph * scale)))
        page_pr.set("landscape", "WIDELY")
        page_pr.find("hp:margin", NS).attrib.update({k: "0" for k in page_pr.find("hp:margin", NS).attrib})
        anchor_run.insert(0, sec_pr)
        ctrl = _child(anchor_run, "hp:ctrl")
        ctrl.append(deepcopy(col_pr_template))
        for z, block in enumerate(page.get("blocks", [])):
            box = _box(block, page)
            if box is None:
                continue
            kind = block.get("type")
            if kind == "text":
                preview.append(_text(block.get("text", "")))
                continue
            run = _child(anchor, "hp:run", charPrIDRef=anchor_char)
            if kind == "table":
                builder.table(run, block, box, scale, z)
                preview.extend("\t".join(map(_text, row)) for row in block.get("rows", []) if isinstance(row, list))
            elif kind == "shape":
                builder.shape(run, block, box, scale, z)
            elif kind == "image":
                picture_counter += 1
                binary_id = "image" + str(picture_counter)
                name = "BinData/" + binary_id + ".png"
                image_payload = _image_bytes(block, page, asset_root, document.get("id"))
                if bounded_images:
                    image_bytes_total = _image_budget(image_bytes_total, image_payload)
                archive.writestr(name, image_payload)
                del image_payload
                _child(manifest, "opf:item", id=binary_id, href=name, **{"media-type": "image/png", "isEmbeded": "1"})
                builder.picture(run, box, scale, binary_id, z,
                                block.get("role") == "graphics-background" or _picture_overlaps_text(box, page))
            else:
                anchor.remove(run)
        cursor = 100
        for line in _body_lines(page):
            cursor = builder.body_text(section, line, scale, pw, cursor)
        name = "Contents/section" + str(page_index) + ".xml"
        section_names.append(name)
        archive.writestr(name, _xml(section))
        _child(manifest, "opf:item", id="section" + str(page_index), href=name, **{"media-type": "application/xml"})
        _child(spine, "opf:itemref", idref="section" + str(page_index), linear="yes")
    # A stale template preview would show unrelated content. Prefer page 1's
    # source thumbnail; the authoritative editable content lives in sections.
    parts.pop("Preview/PrvImage.png", None)
    if pages[0].get("image_url"):
        path = _asset_path(pages[0]["image_url"], asset_root, document.get("id"))
        with Image.open(path) as image:
            thumbnail = image.convert("RGB")
            thumbnail.thumbnail((512, 512))
            output = BytesIO()
            thumbnail.save(output, "PNG")
            image_payload = output.getvalue()
            if bounded_images:
                image_bytes_total = _image_budget(image_bytes_total, image_payload)
            archive.writestr("Preview/PrvImage.png", image_payload)
            del image_payload
    parts["Preview/PrvText.txt"] = "\n".join(preview).encode("utf-8")
    settings = ET.fromstring(parts["settings.xml"])
    caret = settings.find("ha:CaretPosition", NS)
    if caret is not None:
        caret.set("paraIDRef", "1001")
        caret.set("pos", "0")
    parts["settings.xml"] = _xml(settings)
    parts["Contents/header.xml"] = _xml(header)
    parts["Contents/content.hpf"] = _xml(package)
    # Hancom Viewer 2024 hangs at 99% then exits when the hasPart namespace
    # declaration is hoisted to the RDF root with a different prefix. Generic
    # XML/RDF readers accept that equivalent serialization. Preserve Hancom's
    # observed child-scoped ns0 serialization, including for additional pages.
    rdf = ['<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>',
           '<rdf:RDF xmlns:rdf="' + NS["rdf"] + '">']
    for name, kind in [("Contents/header.xml", "HeaderFile")] + [(n, "SectionFile") for n in section_names]:
        rdf.append('<rdf:Description rdf:about=""><ns0:hasPart xmlns:ns0="' + NS["pkg"] + '" rdf:resource="' + name + '"/></rdf:Description>')
        rdf.append('<rdf:Description rdf:about="' + name + '"><rdf:type rdf:resource="' + NS["pkg"] + kind + '"/></rdf:Description>')
    rdf.append('<rdf:Description rdf:about=""><rdf:type rdf:resource="' + NS["pkg"] + 'Document"/></rdf:Description></rdf:RDF>')
    parts["META-INF/container.rdf"] = "".join(rdf).encode("utf-8")
    # ODF manifest is normally empty in Hancom output; content.hpf is the
    # authoritative item manifest. Preserve the standard container definitions.
    for name, value in parts.items():
        if name != "mimetype":
            archive.writestr(name, value)


def export_hwpx(document: dict, asset_root: Path) -> bytes:
    """Compatibility bytes API; production uses export_hwpx_file for bounded RAM."""
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        _write_hwpx(document, asset_root, archive, bounded_images=True)
    return output.getvalue()


def _html_fragments(document: dict, asset_root: Path, *, bounded_images: bool):
    """Yield portable HTML, encoding PNG data in independent 48 KiB chunks."""
    yield "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
    yield '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
    yield "<title>" + escape(_text(document.get("name", "변환 문서"))) + "</title>"
    yield "<style>body{margin:0;background:#ddd;font-family:'Malgun Gothic',sans-serif}.page{position:relative;background:white;margin:20px auto;page-break-after:always}.block{position:absolute;box-sizing:border-box;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1}table{border-collapse:collapse;table-layout:fixed;white-space:pre-wrap}td{border:1px solid #000;padding:1px 2px;vertical-align:top;overflow-wrap:anywhere}[contenteditable]:focus{outline:2px solid #2563eb}@media print{body{background:white}.page{margin:0;break-after:page}}</style></head><body>"
    image_bytes_total = 0
    for page in document.get("pages", []):
        if any(block.get("role") == "graphics-background" for block in page.get("blocks", [])):
            from .editable_graphics import upgrade_legacy_page
            page = upgrade_legacy_page(page, asset_root, document.get("id"))
        pw, ph, _ = _page_geometry(page)
        yield f'<section class="page" style="width:{pw:g}px;height:{ph:g}px">'
        for block in page.get("blocks", []):
            box = _box(block, page)
            if box is None:
                continue
            x, y, w, h = box
            fs = max(2, min(200, _number(block.get("font_size"), 20)))
            layer = 2 if block.get("type") in {"text", "table"} else 0
            style = f"left:{x:g}px;top:{y:g}px;width:{w:g}px;height:{h:g}px;font-size:{fs:g}px;z-index:{layer}"
            kind = block.get("type")
            if kind == "text":
                yield f'<div class="block" contenteditable="plaintext-only" style="{style}">{escape(_text(block.get("text", "")))}</div>'
            elif kind == "image":
                image_payload = _image_bytes(block, page, asset_root, document.get("id"))
                if bounded_images:
                    image_bytes_total = _image_budget(image_bytes_total, image_payload)
                yield f'<img class="block" alt="원본 그림" style="{style}" src="data:image/png;base64,'
                # The chunk length is divisible by three: concatenation has no
                # intermediate base64 padding and decodes to one complete PNG.
                for start in range(0, len(image_payload), 48 * 1024):
                    yield base64.b64encode(image_payload[start:start + 48 * 1024]).decode("ascii")
                yield '">'
                del image_payload
            elif kind == "shape":
                shape = block.get("shape", "rect")
                stroke = _color(block.get("stroke_color")) if block.get("stroke_color", "#000000") is not None and _number(block.get("stroke_width"), 1) > 0 else "none"
                fill = _color(block.get("fill_color"), "none")
                line_width = max(.01, min(200, _number(block.get("stroke_width"), 1)))
                shape_style = f'stroke="{stroke}" stroke-width="{line_width:g}" fill="{fill}"'
                if block.get("stroke_style") in {"DOT", "DASH"}:
                    dash = line_width * (1 if block['stroke_style'] == 'DOT' else 4)
                    shape_style += f' stroke-dasharray="{dash:g} {line_width * 2:g}"'
                yield f'<svg xmlns="http://www.w3.org/2000/svg" class="block" aria-label="편집 가능한 양식 도형" style="{style};overflow:visible" viewBox="0 0 {w:g} {h:g}">'
                points, segments, closed = _shape_geometry(block, box)
                original_rect = shape == "rect" and box == tuple(_number(block.get(key)) for key in ("x", "y", "width", "height"))
                if original_rect:
                    yield f'<rect width="{w:g}" height="{h:g}" {shape_style}/>'
                else:
                    if closed:
                        attrs = " ".join(f"{px:g},{py:g}" for px, py in points)
                        yield f'<polygon points="{attrs}" {shape_style}/>'
                    elif segments:
                        if all(segments[i][1] == segments[i + 1][0] for i in range(len(segments) - 1)):
                            local = [segments[0][0], *(b for _, b in segments)]
                            attrs = " ".join(f"{px:g},{py:g}" for px, py in local)
                            yield f'<polyline points="{attrs}" {shape_style}/>'
                        else:
                            path = " ".join(f"M{a[0]:g},{a[1]:g} L{b[0]:g},{b[1]:g}" for a, b in segments)
                            yield f'<path d="{path}" {shape_style}/>'
                yield '</svg>'
            elif kind == "table":
                rows, spans, covered, xs, ys = _table_grid(block, box)
                yield f'<table class="block" style="{style}"><colgroup>'
                for i in range(len(rows[0])):
                    yield f'<col style="width:{(xs[i + 1] - xs[i]) / w * 100:g}%">'
                yield "</colgroup><tbody>"
                cells = block.get("cells", [])
                cell_styles = {(int(_number(c.get("row"), -1)), int(_number(c.get("col"), -1))): c
                               for c in (cells if isinstance(cells, list) else []) if isinstance(c, dict)}
                for r, values in enumerate(rows):
                    yield f'<tr style="height:{ys[r + 1] - ys[r]:g}px">'
                    for c, text in enumerate(values):
                        if (r, c) in covered:
                            continue
                        rs, cs = spans.get((r, c), (1, 1))
                        cell = {**block, **cell_styles.get((r, c), {})}
                        border = _color(cell.get("border_color"))
                        border_width = max(0, min(200, _number(cell.get("border_width"), 1)))
                        if cell.get("border_color", "#000000") is None:
                            border_width = 0
                        fill = _color(cell.get("fill_color"), "transparent")
                        edge_style = ""
                        borders = cell.get("borders") if isinstance(cell.get("borders"), dict) else {}
                        for side in ("left", "right", "top", "bottom"):
                            edge = borders.get(side)
                            if not isinstance(edge, dict):
                                continue
                            color = _color(edge.get("color", border))
                            width = max(0, min(200, _number(edge.get("width"), border_width)))
                            if edge.get("color", color) is None:
                                width = 0
                            line_style = {'DOT': 'dotted', 'DASH': 'dashed'}.get(edge.get('style', cell.get('border_style')), 'solid')
                            edge_style += f';border-{side}:{width:g}px {line_style} {color}'
                        line_style = {'DOT': 'dotted', 'DASH': 'dashed'}.get(cell.get('border_style'), 'solid')
                        yield f'<td contenteditable="plaintext-only" rowspan="{rs}" colspan="{cs}" style="border:{border_width:g}px {line_style} {border};background:{fill}{edge_style}">{escape(text)}</td>'
                    yield "</tr>"
                yield "</tbody></table>"
        yield "</section>"
    yield "</body></html>"


def export_html(document: dict, asset_root: Path) -> bytes:
    """Compatibility bytes API; file export does not accumulate document strings."""
    return "".join(_html_fragments(document, asset_root, bounded_images=True)).encode("utf-8")


def _atomic_export(destination: Path, writer) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, prefix="." + destination.name + ".", suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        writer(temporary_path)
        temporary_path.replace(destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return destination


def export_hwpx_file(document: dict, asset_root: Path, destination: Path) -> Path:
    """Write one editable HWPX to disk, without an aggregate image RAM buffer."""
    def write(path):
        with ZipFile(path, "w", compression=ZIP_DEFLATED, allowZip64=True) as archive:
            _write_hwpx(document, asset_root, archive, bounded_images=False)
    return _atomic_export(destination, write)


def export_html_file(document: dict, asset_root: Path, destination: Path) -> Path:
    """Write one self-contained HTML file page by page; no 256 MiB aggregate cap."""
    def write(path):
        with path.open("wb") as output:
            for fragment in _html_fragments(document, asset_root, bounded_images=False):
                output.write(fragment.encode("utf-8"))
    return _atomic_export(destination, write)
