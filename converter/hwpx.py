"""Portable, spatial HWPX and editable HTML exports.

HWPX uses editable floating drawing text, actual table cells and embedded PNGs.
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
from io import BytesIO
import math
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from PIL import Image

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


def _number(value, default=0.0) -> float:
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return default
    return value if math.isfinite(value) else default


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
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


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
        borders = header.find("hh:refList/hh:borderFills", NS)
        border = deepcopy(borders[0])
        border.set("id", "3")
        for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
            border.find("hh:" + side, NS).set("type", "SOLID")
        borders.append(border)
        borders.set("itemCnt", str(len(borders)))

    def id(self):
        self.counter += 1
        return str(self.counter)

    def char(self, block, scale):
        size = max(100, min(10000, round(_number(block.get("font_size"), 20) * scale)))
        bold = bool(block.get("bold", False))
        color = str(block.get("color", "#000000"))
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            color = "#000000"
        key = (size, bold, color)
        if key not in self.char_ids:
            prop = deepcopy(self.base_char)
            ident = str(len(self.chars))
            prop.set("id", ident)
            prop.set("height", str(size))
            prop.set("textColor", color)
            prop.find("hh:fontRef", NS).attrib.update({k: "0" for k in prop.find("hh:fontRef", NS).attrib})
            if bold:
                _child(prop, "hh:bold")
            self.chars.append(prop)
            self.chars.set("itemCnt", str(len(self.chars)))
            self.char_ids[key] = ident
        return self.char_ids[key]

    def paragraph(self, parent, text, char_id="0"):
        p = _child(parent, "hp:p", id=self.id(), paraPrIDRef="0", styleIDRef="0", pageBreak="0", columnBreak="0", merged="0")
        run = _child(p, "hp:run", charPrIDRef=char_id)
        t = _child(run, "hp:t")
        pieces = _text(text).split("\n")
        t.text = pieces[0]
        for piece in pieces[1:]:
            br = _child(t, "hp:lineBreak")
            br.tail = piece
        return p

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

    def text(self, run, block, box, scale, zorder):
        shape, w, h = self.drawing(run, "rect", box, scale, zorder)
        shape.set("ratio", "0")
        _child(shape, "hp:lineShape", color="#000000", width="0", style="NONE", endCap="FLAT", headStyle="NORMAL", tailStyle="NORMAL", headfill="0", tailfill="0", headSz="SMALL_SMALL", tailSz="SMALL_SMALL", outlineStyle="NORMAL", alpha="0")
        _child(shape, "hp:shadow", type="NONE", color="#B2B2B2", offsetX="0", offsetY="0", alpha="0")
        draw = _child(shape, "hp:drawText", lastWidth=w, name=_text(block.get("id", "text")), editable="1")
        sub = self.sublist(draw, block.get("text", ""), self.char(block, scale), w, h)
        if "\n" not in _text(block.get("text", "")):
            sub.set("lineWrap", "KEEP")
        ET.SubElement(draw, _tag("hp:textMargin"), dict(_MARGIN))
        for i, (px, py) in enumerate(((0, 0), (w, 0), (w, h), (0, h))):
            _child(shape, "hc:pt" + str(i), x=px, y=py)
        self.floating_position(shape, box, scale)

    def picture(self, run, box, scale, binary_id, zorder):
        shape, w, h = self.drawing(run, "pic", box, scale, zorder)
        shape.set("reverse", "0")
        rect = _child(shape, "hp:imgRect")
        for i, (px, py) in enumerate(((0, 0), (w, 0), (w, h), (0, h))):
            _child(rect, "hc:pt" + str(i), x=px, y=py)
        _child(shape, "hp:imgClip", left="0", right=w, top="0", bottom=h)
        ET.SubElement(shape, _tag("hp:inMargin"), dict(_MARGIN))
        _child(shape, "hp:imgDim", dimwidth=w, dimheight=h)
        _child(shape, "hc:img", binaryItemIDRef=binary_id, bright="0", contrast="0", effect="REAL_PIC", alpha="0")
        _child(shape, "hp:effects")
        self.floating_position(shape, box, scale)

    def table(self, run, block, box, scale, zorder):
        rows, spans, covered, xs, ys = _table_grid(block, box)
        table = _child(run, "hp:tbl", id=self.id(), zOrder=zorder, numberingType="TABLE", textWrap="IN_FRONT_OF_TEXT", textFlow="BOTH_SIDES", lock="0", dropcapstyle="None", pageBreak="NONE", repeatHeader="0", rowCnt=len(rows), colCnt=len(rows[0]), cellSpacing="0", borderFillIDRef="3", noAdjust="1")
        self.floating_position(table, box, scale)
        ET.SubElement(table, _tag("hp:inMargin"), dict(_MARGIN))
        char_id = self.char(block, scale)
        for r, values in enumerate(rows):
            tr = _child(table, "hp:tr")
            for c, text in enumerate(values):
                if (r, c) in covered:
                    continue
                rs, cs = spans.get((r, c), (1, 1))
                w = max(1, round(xs[c + cs] * scale) - round(xs[c] * scale))
                h = max(1, round(ys[r + rs] * scale) - round(ys[r] * scale))
                tc = _child(tr, "hp:tc", name="", header="0", hasMargin="1", protect="0", editable="1", dirty="0", borderFillIDRef="3")
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
        pw, ph, scale = _page_geometry(page)
        section = ET.Element(_tag("hs:sec"))
        anchor = builder.paragraph(section, "")
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
            run = _child(anchor, "hp:run", charPrIDRef="0")
            kind = block.get("type")
            if kind == "text":
                builder.text(run, block, box, scale, z)
                preview.append(_text(block.get("text", "")))
            elif kind == "table":
                builder.table(run, block, box, scale, z)
                preview.extend("\t".join(map(_text, row)) for row in block.get("rows", []) if isinstance(row, list))
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
                builder.picture(run, box, scale, binary_id, z)
            else:
                anchor.remove(run)
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
        pw, ph, _ = _page_geometry(page)
        yield f'<section class="page" style="width:{pw:g}px;height:{ph:g}px">'
        for block in page.get("blocks", []):
            box = _box(block, page)
            if box is None:
                continue
            x, y, w, h = box
            fs = max(2, min(200, _number(block.get("font_size"), 20)))
            style = f"left:{x:g}px;top:{y:g}px;width:{w:g}px;height:{h:g}px;font-size:{fs:g}px"
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
            elif kind == "table":
                rows, spans, covered, xs, ys = _table_grid(block, box)
                yield f'<table class="block" style="{style}"><colgroup>'
                for i in range(len(rows[0])):
                    yield f'<col style="width:{(xs[i + 1] - xs[i]) / w * 100:g}%">'
                yield "</colgroup><tbody>"
                for r, values in enumerate(rows):
                    yield f'<tr style="height:{ys[r + 1] - ys[r]:g}px">'
                    for c, text in enumerate(values):
                        if (r, c) in covered:
                            continue
                        rs, cs = spans.get((r, c), (1, 1))
                        yield f'<td contenteditable="plaintext-only" rowspan="{rs}" colspan="{cs}">{escape(text)}</td>'
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
