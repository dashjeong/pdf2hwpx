"""Exporter invariants: package references, editable objects, layout and safety."""
import base64
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import random
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_STORED
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw

from converter.hwpx import NS, export_html, export_hwpx, export_html_file, export_hwpx_file, _asset_path


def fixture():
    return {
        "id": "job-123", "name": "한글 & <수정한 문서>",
        "pages": [
            {"index": 0, "width": 600, "height": 900, "dpi": 150,
             "image_url": "/api/assets/job-123/page.png", "blocks": [
                 {"id": "t1", "type": "text", "x": 20, "y": 30, "width": 300, "height": 50,
                  "font_size": 24, "text": "수정한 한글 123 & <검증>\n두 번째 줄"},
                 {"id": "table1", "type": "table", "x": 40, "y": 200, "width": 400, "height": 120,
                  "rows": [["이름", "편집한 금액"], ["홍길동", "99,000"]],
                  "cells": [{"row": 0, "col": 0, "x": 40, "y": 200, "width": 100, "height": 60},
                            {"row": 0, "col": 1, "x": 140, "y": 200, "width": 300, "height": 60}]},
                 {"id": "i1", "type": "image", "x": 150, "y": 400, "width": 200, "height": 100,
                  "image_url": "/api/assets/job-123/crop.png"},
             ]},
            {"index": 1, "width": 900, "height": 600, "dpi": 150, "blocks": [
                {"id": "t2", "type": "text", "x": 50, "y": 50, "width": 250, "height": 30,
                 "font_size": 18, "text": "가로 페이지"}
            ]},
        ]
    }


class HwpxExportTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "build" / "export-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "job-123").mkdir()
        Image.new("RGB", (600, 900), "white").save(self.root / "job-123" / "page.png")
        Image.new("RGB", (100, 50), "orange").save(self.root / "job-123" / "crop.png")
        self.doc = fixture()

    def archive(self, doc=None):
        return ZipFile(BytesIO(export_hwpx(doc or self.doc, self.root)))

    def test_package_references_and_xml_are_valid(self):
        with self.archive() as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.namelist()[0], "mimetype")
            self.assertEqual(z.getinfo("mimetype").compress_type, ZIP_STORED)
            self.assertEqual(z.read("mimetype"), b"application/hwp+zip")
            # Native Hancom Viewer requires this observed RDF serialization;
            # generic XML validation cannot detect the equivalent-prefix crash.
            self.assertIn(b'<ns0:hasPart xmlns:ns0=', z.read("META-INF/container.rdf"))
            self.assertNotIn(b'<pkg:hasPart', z.read("META-INF/container.rdf"))
            for name in z.namelist():
                if name.endswith((".xml", ".hpf", ".rdf")):
                    ET.fromstring(z.read(name))
            hpf = ET.fromstring(z.read("Contents/content.hpf"))
            manifest = hpf.findall("opf:manifest/opf:item", NS)
            item_ids = {item.get("id") for item in manifest}
            self.assertEqual(len(item_ids), len(manifest))
            for item in manifest:
                self.assertIn(item.get("href"), z.namelist())
            for item in hpf.findall("opf:spine/opf:itemref", NS):
                self.assertIn(item.get("idref"), item_ids)
            header = ET.fromstring(z.read("Contents/header.xml"))
            self.assertEqual(header.get("secCnt"), "2")
            self.assertIn("BinData/image1.png", z.namelist())
            for name in ("Contents/section0.xml", "Contents/section1.xml"):
                self.assertIn(b'standalone="yes"', z.read(name).split(b"?>", 1)[0])
                for prefix in ("ha", "hp10", "hh", "hpf", "config"):
                    self.assertIn(f"xmlns:{prefix}=".encode(), z.read(name).split(b">", 2)[1])
                section = ET.fromstring(z.read(name))
                for image in section.findall(".//hc:img", NS):
                    self.assertIn(image.get("binaryItemIDRef"), item_ids)
                char_ids = {c.get("id") for c in header.findall(".//hh:charPr", NS)}
                for run in section.findall(".//hp:run", NS):
                    self.assertIn(run.get("charPrIDRef"), char_ids)

    def test_geometry_edited_text_fonts_and_table_cells_survive(self):
        with self.archive() as z:
            section = ET.fromstring(z.read("Contents/section0.xml"))
            page = section.find(".//hp:pagePr", NS)
            self.assertEqual((page.get("width"), page.get("height")), ("28800", "43200"))
            body = section.findall("hp:p", NS)[1:]
            self.assertEqual(len(body), 2)
            self.assertEqual("".join(body[0].itertext()), "수정한 한글 123 & <검증>")
            self.assertEqual("".join(body[1].itertext()), "두 번째 줄")
            line = body[0].find("hp:linesegarray/hp:lineseg", NS)
            self.assertEqual(line.get("horzpos"), "960")
            self.assertEqual(line.get("vertpos"), "1440")
            self.assertEqual(section.findall(".//hp:rect", NS), [])
            self.assertEqual(section.findall(".//hp:drawText", NS), [])
            self.assertNotIn("<검증>", z.read("Contents/section0.xml").decode())
            table = section.find(".//hp:tbl", NS)
            self.assertEqual((table.get("rowCnt"), table.get("colCnt")), ("2", "2"))
            cells = table.findall("hp:tr/hp:tc", NS)
            self.assertEqual(len(cells), 4)
            self.assertTrue(all(c.get("editable") == "1" for c in cells))
            self.assertEqual(cells[0].find("hp:cellSz", NS).get("width"), "4800")
            self.assertEqual(cells[1].find("hp:cellSz", NS).get("width"), "14400")
            self.assertIn("편집한 금액", "".join(table.itertext()))
            self.assertIn("99,000", "".join(table.itertext()))
            header = ET.fromstring(z.read("Contents/header.xml"))
            self.assertTrue(all(f.get("face") == "맑은 고딕" for f in header.findall(".//hh:font", NS)))
            body_run = body[0].find("hp:run", NS)
            char = next(c for c in header.findall(".//hh:charPr", NS) if c.get("id") == body_run.get("charPrIDRef"))
            self.assertEqual(char.get("height"), "1152")
            para = next(p for p in header.findall(".//hh:paraPr", NS) if p.get("id") == body[0].get("paraPrIDRef"))
            self.assertTrue(all(m.find("hc:left", NS).get("value") == "960" for m in para.findall(".//hh:margin", NS)))
            second = ET.fromstring(z.read("Contents/section1.xml"))
            page = second.find(".//hp:pagePr", NS)
            self.assertEqual((page.get("width"), page.get("height")), ("43200", "28800"))
            self.assertEqual(second.find("hp:p", NS).get("pageBreak"), "1")

    def test_merged_cells_use_browser_edited_rows(self):
        doc = deepcopy(self.doc)
        block = doc["pages"][0]["blocks"][1]
        block["rows"] = [["브라우저 수정", ""], ["첫 칸", "둘째 칸"]]
        block["cells"] = [{"row": 0, "col": 0, "rowspan": 1, "colspan": 2, "text": "옛 OCR 값"}]
        with self.archive(doc) as z:
            table = ET.fromstring(z.read("Contents/section0.xml")).find(".//hp:tbl", NS)
            cells = table.findall("hp:tr/hp:tc", NS)
            self.assertEqual(len(cells), 3)
            self.assertEqual(cells[0].find("hp:cellSpan", NS).get("colSpan"), "2")
            self.assertIn("브라우저 수정", "".join(table.itertext()))
            self.assertNotIn("옛 OCR 값", "".join(table.itertext()))
        html = export_html(doc, self.root).decode()
        self.assertIn('colspan="2"', html)
        self.assertIn("브라우저 수정", html)

    def test_clip_out_of_page_and_reject_nonfinite_page(self):
        doc = deepcopy(self.doc)
        doc["pages"][0]["blocks"][0].update(x=-10, y=890, width=100, height=40)
        with self.archive(doc) as z:
            body = ET.fromstring(z.read("Contents/section0.xml")).findall("hp:p", NS)[1:]
            line = body[0].find("hp:linesegarray/hp:lineseg", NS)
            self.assertEqual(line.get("horzpos"), "0")
            self.assertEqual(line.get("vertpos"), "42720")
            self.assertIn("수정한 한글", "".join(body[0].itertext()))
        doc["pages"][0]["width"] = float("nan")
        with self.assertRaises(ValueError):
            export_hwpx(doc, self.root)

    def test_asset_urls_reject_traversal_external_other_job_and_missing(self):
        for url in (
            "https://example.com/image.png", "//example.com/image.png",
            "/api/assets/job-123/../page.png", "/api/assets/job-123/%2e%2e",
            "/api/assets/job-123/%2e%2e%5csecret", "/api/assets/job-123/a%2fsecret",
            "/api/assets/job-123/C:%5csecret", "/api/assets/job-456/page.png",
            "/api/assets/job-123/page.png?query=1", "/api/assets/job-123/missing.png",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _asset_path(url, self.root, "job-123")

    def test_html_is_portable_editable_and_escapes_injection(self):
        doc = deepcopy(self.doc)
        doc["pages"][0]["blocks"][0]["text"] = '<script>alert("x")</script> & 한글'
        html = export_html(doc, self.root).decode()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("contenteditable=\"plaintext-only\"", html)
        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn("/api/assets/", html)
        payload = html.split("data:image/png;base64,", 1)[1].split('"', 1)[0]
        with Image.open(BytesIO(base64.b64decode(payload))) as image:
            self.assertEqual(image.size, (100, 50))

    def test_removes_illegal_xml_controls_preserves_tabs_and_newlines(self):
        doc = deepcopy(self.doc)
        doc["pages"][0]["blocks"][0]["text"] = "한글\x00\x08\t1\n다음"
        with self.archive(doc) as z:
            section = ET.fromstring(z.read("Contents/section0.xml"))
            self.assertIn("한글1", "".join(section.itertext()))
            self.assertEqual(len(section.findall(".//hp:t/hp:tab", NS)), 1)
            self.assertEqual(["".join(p.itertext()) for p in section.findall("hp:p", NS)[1:]], ["한글1", "다음"])

    def _two_image_pages(self):
        # Each image fits by itself; their combined payload crosses the budget.
        # Put the second image on another page to exercise document-wide bounds.
        doc = deepcopy(self.doc)
        page = doc["pages"][0]
        page.pop("image_url")
        page["blocks"] = [page["blocks"][2]]
        doc["pages"] = [page]
        single = deepcopy(doc)
        doc["pages"].append(deepcopy(page))
        png_size = (self.root / "job-123" / "crop.png").stat().st_size
        return single, doc, png_size + png_size // 2

    def test_hwpx_rejects_aggregate_images_across_pages(self):
        single, aggregate, cap = self._two_image_pages()
        with patch("converter.hwpx.MAX_EMBEDDED_IMAGE_BYTES", cap):
            with self.archive(single) as archive:
                self.assertIn("BinData/image1.png", archive.namelist())
            with self.assertRaisesRegex(ValueError, "변환할 쪽을 나눠"):
                export_hwpx(aggregate, self.root)

    def test_html_rejects_aggregate_images_across_pages(self):
        single, aggregate, cap = self._two_image_pages()
        with patch("converter.hwpx.MAX_EMBEDDED_IMAGE_BYTES", cap):
            self.assertIn(b"data:image/png;base64,", export_html(single, self.root))
            with self.assertRaisesRegex(ValueError, "변환할 쪽을 나눠"):
                export_html(aggregate, self.root)

    def test_stream_hwpx_keeps_one_document_beyond_bytes_api_budget(self):
        _, doc, cap = self._two_image_pages()
        for index, page in enumerate(doc["pages"]):
            page["blocks"].append({"type": "text", "x": 20, "y": 30, "width": 400, "height": 50,
                                   "text": "편집한 쪽 " + str(index + 1), "font_size": 20 + index * 8})
        destination = self.root / "combined.hwpx"
        with patch("converter.hwpx.MAX_EMBEDDED_IMAGE_BYTES", cap):
            with self.assertRaises(ValueError):
                export_hwpx(doc, self.root)
            self.assertEqual(export_hwpx_file(doc, self.root, destination), destination)
        with ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist()[0], "mimetype")
            header = ET.fromstring(archive.read("Contents/header.xml"))
            self.assertEqual(header.get("secCnt"), "2")
            char_ids = {c.get("id") for c in header.findall(".//hh:charPr", NS)}
            for index in range(2):
                section = ET.fromstring(archive.read(f"Contents/section{index}.xml"))
                self.assertIn("편집한 쪽 " + str(index + 1), "".join(section.itertext()))
                self.assertTrue(all(r.get("charPrIDRef") in char_ids for r in section.findall(".//hp:run", NS)))
                self.assertEqual(section.find(".//hc:img", NS).get("binaryItemIDRef"), "image" + str(index + 1))
                with Image.open(BytesIO(archive.read(f"BinData/image{index + 1}.png"))) as image:
                    self.assertEqual(image.size, (100, 50))

    def test_stream_html_base64_chunks_reconstruct_full_png_across_pages(self):
        pixels = random.Random(42).randbytes(256 * 256 * 3)
        crop = self.root / "job-123" / "crop.png"
        Image.frombytes("RGB", (256, 256), pixels).save(crop)
        _, doc, cap = self._two_image_pages()
        destination = self.root / "combined.html"
        with patch("converter.hwpx.MAX_EMBEDDED_IMAGE_BYTES", cap):
            with self.assertRaises(ValueError):
                export_html(doc, self.root)
            self.assertEqual(export_html_file(doc, self.root, destination), destination)
        html = destination.read_text(encoding="utf-8")
        self.assertEqual(html.count('<section class="page"'), 2)
        payloads = [part.split('"', 1)[0] for part in html.split("data:image/png;base64,")[1:]]
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            raw = base64.b64decode(payload, validate=True)
            self.assertGreater(len(raw), 48 * 1024 * 3)
            with Image.open(BytesIO(raw)) as image:
                self.assertEqual(image.tobytes(), pixels)
        self.assertNotIn("/api/assets/", html)

    def test_stream_failure_preserves_previous_file_and_removes_partial(self):
        _, doc, _ = self._two_image_pages()
        doc["pages"][1]["blocks"][0]["image_url"] = "/api/assets/job-123/missing.png"
        for extension, exporter in (("hwpx", export_hwpx_file), ("html", export_html_file)):
            with self.subTest(extension=extension):
                destination = self.root / ("previous." + extension)
                destination.write_bytes(b"previous complete output")
                with self.assertRaises(ValueError):
                    exporter(doc, self.root, destination)
                self.assertEqual(destination.read_bytes(), b"previous complete output")
                self.assertEqual(list(self.root.glob("." + destination.name + ".*.tmp")), [])

    def test_large_raster_dpi_preserves_physical_width_and_original_coordinates(self):
        dpi = 20000 / 11.6929
        scale = 7200 / dpi
        doc = {"pages": [{"width": 20000, "height": 10000, "dpi": dpi, "blocks": [
            {"type": "text", "x": 10000, "y": 3000, "width": 1500, "height": 400,
             "font_size": 240, "text": "큰 이미지 원본 좌표"}
        ]}]}
        with self.archive(doc) as archive:
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            geometry = section.find(".//hp:pagePr", NS)
            self.assertEqual(geometry.get("width"), str(round(11.6929 * 7200)))
            self.assertEqual(geometry.get("height"), str(round(10000 * scale)))
            body = section.findall("hp:p", NS)[1]
            line = body.find("hp:linesegarray/hp:lineseg", NS)
            self.assertEqual(line.get("horzpos"), str(round(10000 * scale)))
            self.assertEqual(line.get("vertpos"), str(round(3000 * scale)))
            header = ET.fromstring(archive.read("Contents/header.xml"))
            char_id = body.find("hp:run", NS).get("charPrIDRef")
            char = next(c for c in header.findall(".//hh:charPr", NS) if c.get("id") == char_id)
            self.assertEqual(char.get("height"), str(round(240 * scale)))
        doc["pages"][0]["width"] = 1000001
        with self.assertRaises(ValueError):
            export_hwpx(doc, self.root)

    def test_font_fragments_join_one_body_sentence_without_cells(self):
        doc = deepcopy(self.doc)
        page = doc["pages"][0]
        page["blocks"] = [
            {"type": "text", "x": 20, "y": 30, "width": 80, "height": 24, "font_size": 20, "text": "한글 문장"},
            {"type": "text", "x": 106, "y": 31, "width": 60, "height": 22, "font_size": 20, "text": "123", "bold": True},
            {"type": "text", "x": 240, "y": 30, "width": 120, "height": 24, "font_size": 20, "text": "같은 줄 끝"},
        ]
        with self.archive(doc) as archive:
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            paragraphs = section.findall("hp:p", NS)[1:]
            self.assertEqual(len(paragraphs), 1)
            self.assertEqual("".join(paragraphs[0].itertext()), "한글 문장 123같은 줄 끝")
            runs = paragraphs[0].findall("hp:run", NS)
            self.assertEqual(len(runs), 3)
            self.assertNotEqual(runs[0].get("charPrIDRef"), runs[1].get("charPrIDRef"))
            self.assertEqual(len(paragraphs[0].findall("hp:run/hp:t/hp:tab", NS)), 1)
            for name in ("tbl", "tc", "rect", "drawText", "subList"):
                self.assertEqual(section.findall(".//hp:" + name, NS), [])
            header = ET.fromstring(archive.read("Contents/header.xml"))
            para = next(p for p in header.findall(".//hh:paraPr", NS) if p.get("id") == paragraphs[0].get("paraPrIDRef"))
            tabs = next(t for t in header.findall(".//hh:tabPr", NS) if t.get("id") == para.get("tabPrIDRef"))
            self.assertEqual(tabs.find("hh:tabItem", NS).get("pos"), "11520")

    def test_only_actual_tables_use_cells_and_old_background_upgrades_to_form_shapes(self):
        doc = deepcopy(self.doc)
        doc["pages"][0]["blocks"][2]["role"] = "graphics-background"
        source = self.root / "job-123" / "page.png"
        with Image.open(source) as image:
            ImageDraw.Draw(image).line((20, 600, 500, 600), fill="#336699", width=3)
            image.save(source)
        with self.archive(doc) as archive:
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            self.assertEqual(len(section.findall(".//hp:tbl", NS)), 1)
            self.assertEqual(len(section.findall(".//hp:tc", NS)), 4)
            self.assertIsNotNone(section.find(".//hp:line", NS))
            self.assertEqual(section.find(".//hp:line", NS).get("textWrap"), "BEHIND_TEXT")
            self.assertEqual(section.findall(".//hp:drawText", NS), [])
            # Body text remains a sibling paragraph, never a table-cell sublist.
            self.assertEqual(len(section.findall("hp:p/hp:run/hp:t", NS)), 3)

    def test_many_edited_newlines_in_small_ocr_box_remain_separate_paragraphs(self):
        doc = deepcopy(self.doc)
        lines = ["편집한 줄 " + str(index) for index in range(20)]
        doc["pages"][0]["blocks"] = [{"type": "text", "x": 20, "y": 30,
                                    "width": 400, "height": 20, "font_size": 20,
                                    "text": "\n".join(lines)}]
        with self.archive(doc) as archive:
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            paragraphs = section.findall("hp:p", NS)[1:]
            self.assertEqual(["".join(p.itertext()) for p in paragraphs], lines)
            self.assertEqual(section.findall(".//hp:rect", NS), [])
            self.assertEqual(section.findall(".//hp:tbl", NS), [])

    def test_same_baseline_mixed_font_sizes_are_one_body_paragraph(self):
        doc = deepcopy(self.doc)
        doc["pages"][0]["blocks"] = [
            {"type": "text", "x": 20, "y": 20, "width": 120, "height": 40,
             "font_size": 40, "text": "보고서", "bold": True},
            {"type": "text", "x": 146, "y": 40, "width": 60, "height": 20,
             "font_size": 20, "text": "2026"},
        ]
        with self.archive(doc) as archive:
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            paragraphs = section.findall("hp:p", NS)[1:]
            self.assertEqual(len(paragraphs), 1)
            self.assertEqual("".join(paragraphs[0].itertext()), "보고서 2026")
            self.assertEqual(len(paragraphs[0].findall("hp:run", NS)), 2)

    def test_form_borders_are_real_shapes_and_cell_colors_are_editable_styles(self):
        doc = deepcopy(self.doc)
        page = doc["pages"][0]
        page["blocks"] += [
            {"type": "shape", "shape": "line", "x": 20, "y": 100, "width": 400, "height": 0,
             "stroke_color": "#336699", "stroke_width": 2},
            {"type": "shape", "shape": "rect", "x": 20, "y": 130, "width": 200, "height": 40,
             "stroke_color": "#FF0000", "stroke_width": 1, "fill_color": "#FFFFCC"},
            {"type": "shape", "shape": "polyline", "x": 30, "y": 500, "width": 200, "height": 50,
             "points": [[0, 0], [80, 40], [200, 20]], "stroke_color": "#000000", "closed": False},
        ]
        table = page["blocks"][1]
        table["border_color"] = "#336699"
        table["border_width"] = 2
        table["cells"][0]["fill_color"] = "#FFFFCC"
        with self.archive(doc) as archive:
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            line = section.find(".//hp:line", NS)
            self.assertIsNotNone(line)
            self.assertEqual(line.find("hc:endPt", NS).get("y"), "0")
            self.assertEqual(line.find("hp:lineShape", NS).get("width"), "96")
            rect = section.find(".//hp:rect", NS)
            self.assertIsNone(rect.find("hp:drawText", NS))
            self.assertEqual(rect.find("hc:fillBrush/hc:winBrush", NS).get("faceColor"), "#FFFFCC")
            curve = section.find(".//hp:curve", NS)
            self.assertEqual(len(curve.findall("hp:seg", NS)), 2)
            header = ET.fromstring(archive.read("Contents/header.xml"))
            cell = section.find(".//hp:tbl/hp:tr/hp:tc", NS)
            border = next(b for b in header.findall(".//hh:borderFill", NS) if b.get("id") == cell.get("borderFillIDRef"))
            self.assertEqual(border.find("hh:leftBorder", NS).get("color"), "#336699")
            self.assertEqual(border.find("hc:fillBrush/hc:winBrush", NS).get("faceColor"), "#FFFFCC")
            self.assertEqual(section.findall(".//hp:drawText", NS), [])
        html = export_html(doc, self.root).decode()
        self.assertIn('<svg xmlns=', html)
        self.assertIn('points="0,0 80,40 200,20"', html)
        self.assertIn('background:#FFFFCC', html)

    def test_original_font_width_fit_does_not_shrink_edited_text(self):
        doc = deepcopy(self.doc)
        block = doc["pages"][0]["blocks"][0]
        block.update(source="native-pdf-text", text="원본 문장", width=80, height=30, font_size=20)
        with patch("converter.hwpx._body_width_ratio", return_value=82):
            with self.archive(doc) as archive:
                section = ET.fromstring(archive.read("Contents/section0.xml"))
                run = section.findall("hp:p", NS)[1].find("hp:run", NS)
                header = ET.fromstring(archive.read("Contents/header.xml"))
                char = next(c for c in header.findall(".//hh:charPr", NS) if c.get("id") == run.get("charPrIDRef"))
                self.assertEqual(char.find("hh:ratio", NS).get("hangul"), "82")
                self.assertEqual(char.get("height"), "960")
        from converter.hwpx import _body_width_ratio
        block.update(text_edited=True, text="사용자가 길게 편집한 문장은 원래 글자 크기로 자연스럽게 줄바꿈합니다.")
        self.assertEqual(_body_width_ratio(block, (20, 30, 80, 30)), 100)

    def test_shape_points_are_clipped_with_their_origin_and_table_edges_keep_styles(self):
        doc = deepcopy(self.doc)
        table = doc['pages'][0]['blocks'][1]
        table['cells'][0]['borders'] = {'left': {'color': '#FF0000', 'width': 2},
                                      'right': {'color': '#0000FF', 'width': 0}}
        doc['pages'][0]['blocks'].append({'type': 'shape', 'shape': 'line', 'x': -10, 'y': -10,
                                        'width': 20, 'height': 20, 'points': [[0, 0], [20, 20]],
                                        'stroke_color': '#000000', 'stroke_width': 1})
        with self.archive(doc) as archive:
            section = ET.fromstring(archive.read('Contents/section0.xml'))
            line = section.find('.//hp:line', NS)
            self.assertEqual(line.find('hc:startPt', NS).attrib, {'x': '0', 'y': '0'})
            self.assertEqual(line.find('hc:endPt', NS).attrib, {'x': '480', 'y': '480'})
            header = ET.fromstring(archive.read('Contents/header.xml'))
            cell = section.find('.//hp:tc', NS)
            border = next(b for b in header.findall('.//hh:borderFill', NS) if b.get('id') == cell.get('borderFillIDRef'))
            self.assertEqual(border.find('hh:leftBorder', NS).get('color'), '#FF0000')
            self.assertEqual(border.find('hh:rightBorder', NS).get('type'), 'NONE')
            self.assertEqual(section.find('.//hp:pic/hp:lineShape', NS).get('style'), 'NONE')
        html = export_html(doc, self.root).decode()
        self.assertIn('points="0,0 10,10"', html)
        self.assertNotIn('points="0,0 20,20"', html)
        self.assertIn('border-left:2px solid #FF0000', html)


    def test_native_axis_rules_crossing_page_edges_keep_only_visible_segments(self):
        from contextlib import closing
        import pypdfium2 as pdfium
        from converter.editable_graphics import native_pdf_shapes

        # PDF paths can start outside a normal, unrotated page. Retain visible
        # rules and clip their endpoints, while discarding wholly external ones.
        with closing(pdfium.PdfDocument.new()) as pdf:
            page = pdf.new_page(100, 100)
            try:
                for start, end in [
                    ((-10, 50), (40, 50)), ((50, 110), (50, 60)),
                    ((10, 110), (60, 110)), ((-10, 90), (-10, 40)),
                    ((-60, 30), (-30, 30)), ((70, 160), (70, 130)),
                ]:
                    path = pdfium.raw.FPDFPageObj_CreateNewPath(*start)
                    pdfium.raw.FPDFPath_LineTo(path, *end)
                    pdfium.raw.FPDFPageObj_SetStrokeColor(path, 0, 0, 0, 255)
                    pdfium.raw.FPDFPageObj_SetStrokeWidth(path, 1)
                    pdfium.raw.FPDFPath_SetDrawMode(path, pdfium.raw.FPDF_FILLMODE_NONE, True)
                    pdfium.raw.FPDFPage_InsertObject(page, path)
                self.assertTrue(pdfium.raw.FPDFPage_GenerateContent(page))
                shapes, _ = native_pdf_shapes(page, 100, 100, [])
                self.assertEqual(len(shapes), 6)
                self.assertEqual((page.get_rotation(), page.get_bbox()), (0, (0., 0., 100., 100.)))
                bitmap = page.render(scale=1)
                try:
                    image = bitmap.to_pil()
                    try:
                        self.assertLess(max(image.getpixel((20, 50))), 245)
                        self.assertLess(max(image.getpixel((50, 20))), 245)
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()

        doc = {"id": "job-123", "name": "Page-crossing form rules", "pages": [
            {"width": 100, "height": 100, "dpi": 72, "blocks": shapes}]}
        with self.archive(doc) as archive:
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            lines = section.findall(".//hp:line", NS)
            self.assertEqual(len(lines), 2)
            for line, expected_end, expected_position in zip(
                lines, [("4000", "0"), ("0", "4000")], [("0", "5000"), ("5000", "0")]
            ):
                start, end = line.find("hc:startPt", NS), line.find("hc:endPt", NS)
                position = line.find("hp:pos", NS)
                self.assertEqual((start.get("x"), start.get("y")), ("0", "0"))
                self.assertEqual((end.get("x"), end.get("y")), expected_end)
                self.assertEqual((position.get("horzOffset"), position.get("vertOffset")), expected_position)
            self.assertEqual(section.findall(".//hp:drawText", NS), [])
            self.assertEqual(section.findall(".//hp:pic", NS), [])
        html = export_html(doc, self.root).decode()
        self.assertEqual(html.count("<svg xmlns="), 2)
        self.assertIn('points="0,0 40,0"', html)
        self.assertIn('points="0,0 0,40"', html)
        self.assertIn("left:0px;top:50px;width:40px;height:0.01px", html)
        self.assertIn("left:50px;top:0px;width:0.01px;height:40px", html)
        self.assertNotIn("left:-", html)
        self.assertNotIn("top:-", html)


if __name__ == "__main__":
    unittest.main()
