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

from PIL import Image

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
            rect = section.find(".//hp:rect", NS)
            self.assertEqual(rect.find("hp:pos", NS).get("horzOffset"), "960")
            self.assertEqual(rect.find("hp:pos", NS).get("vertOffset"), "1440")
            self.assertEqual(rect.find("hp:pos", NS).get("horzRelTo"), "PAPER")
            self.assertEqual(rect.find("hp:drawText", NS).get("editable"), "1")
            self.assertIn("수정한 한글 123 & <검증>", "".join(rect.itertext()))
            self.assertIn("두 번째 줄", "".join(rect.itertext()))
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
            rect_run = rect.find(".//hp:run", NS)
            char = next(c for c in header.findall(".//hh:charPr", NS) if c.get("id") == rect_run.get("charPrIDRef"))
            self.assertEqual(char.get("height"), "1152")
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
            rect = ET.fromstring(z.read("Contents/section0.xml")).find(".//hp:rect", NS)
            pos, size = rect.find("hp:pos", NS), rect.find("hp:sz", NS)
            self.assertEqual(pos.get("horzOffset"), "0")
            self.assertEqual(size.get("width"), "4320")
            self.assertEqual(size.get("height"), "480")
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
            self.assertIn("한글\t1", "".join(section.itertext()))
            self.assertEqual(len(section.findall(".//hp:lineBreak", NS)), 1)

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
            rect = section.find(".//hp:rect", NS)
            self.assertEqual(rect.find("hp:pos", NS).get("horzOffset"), str(round(10000 * scale)))
            self.assertEqual(rect.find("hp:pos", NS).get("vertOffset"), str(round(3000 * scale)))
            header = ET.fromstring(archive.read("Contents/header.xml"))
            char_id = rect.find(".//hp:run", NS).get("charPrIDRef")
            char = next(c for c in header.findall(".//hh:charPr", NS) if c.get("id") == char_id)
            self.assertEqual(char.get("height"), str(round(240 * scale)))
        doc["pages"][0]["width"] = 1000001
        with self.assertRaises(ValueError):
            export_hwpx(doc, self.root)


if __name__ == "__main__":
    unittest.main()
