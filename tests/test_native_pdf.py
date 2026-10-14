from collections import Counter
from pathlib import Path
import os
import tempfile
import unittest

import pypdfium2 as pdfium
from PIL import Image

from converter.native_pdf import recognize_native_pdf_page, _cell_text, _glyphs

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PDF = Path(os.environ.get('PRIVATE_PDF', str(ROOT / 'tests' / 'fixtures' / 'private.pdf')))


class NativePdfTests(unittest.TestCase):
    @staticmethod
    def glyph(text, x, y, width=20, height=20, baseline=None):
        return {"text": text, "x": x, "y": y, "width": width, "height": height,
                "baseline": y + height if baseline is None else baseline,
                "font_size": height, "color": "#000000", "bold": False,
                "source_font": "Test"}

    def test_cell_superscript_footnotes_follow_their_header(self):
        label = [self.glyph("서", 0, 4), self.glyph(" ", 20, 4, 5),
                 self.glyph("울", 25, 4), self.glyph(" ", 45, 4, 5),
                 self.glyph("시", 50, 4), self.glyph("*", 70, 1, 8, 13)]
        self.assertEqual(_cell_text(label), "서 울 시*")
        district = [self.glyph(char, i * 20, 30) for i, char in enumerate("자치구")]
        district.extend([self.glyph("*", 60, 27, 8, 13), self.glyph("*", 68, 27, 8, 13)])
        self.assertEqual(_cell_text(district), "자치구**")
        self.assertEqual(Counter("".join(_cell_text(district).split())),
                         Counter("".join(g["text"] for g in district)))

    def test_cell_keeps_ordinary_lines_and_separate_footnote_line(self):
        glyphs = [self.glyph("임", 0, 4), self.glyph("기", 20, 4),
                  self.glyph("제", 10, 34), self.glyph("*", 60, 0, 8, 13)]
        # The remote symbol is not adjacent to the preceding label.
        self.assertEqual(_cell_text(glyphs), "*\n임기\n제")
        self.assertEqual(_cell_text(glyphs[:-1]), "임기\n제")

    def test_widely_spaced_cell_header_stays_on_one_line(self):
        glyphs = [self.glyph(char, index * 60, 4) for index, char in enumerate("주요업무")]
        glyphs.extend([self.glyph("둘", 0, 34), self.glyph("째", 20, 34)])
        self.assertEqual(_cell_text(glyphs), "주 요 업 무\n둘째")

    def test_actual_native_page_five_header_has_no_newlines(self):
        source = PRIVATE_PDF
        if not source.exists():
            self.skipTest("User-supplied PDF unavailable")
        pdf = pdfium.PdfDocument(str(source))
        page = pdf[4]
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as folder:
            path = Path(folder) / "source.png"
            page.render(scale=150 / 72).to_pil().save(path)
            result = recognize_native_pdf_page(page, path, {})
            header = next(cell["text"] for block in result["blocks"] if block["type"] == "table"
                          for cell in block["cells"] if "".join(cell["text"].split()) == "주요업무")
            self.assertNotIn("\n", header)
        page.close()
        pdf.close()

    def test_force_ocr_does_not_read_native_layer(self):
        self.assertIsNone(recognize_native_pdf_page(None, Path("unused.png"), {"force_ocr": True}))

    def test_scan_without_native_text_falls_back(self):
        scan = ROOT / "samples" / "official-memo-scanned.pdf"
        if not scan.exists():
            self.skipTest("Synthetic scan fixture unavailable")
        pdf = pdfium.PdfDocument(str(scan))
        page = pdf[0]
        image = ROOT / "data" / "demo" / "page-1.png"
        self.assertIsNone(recognize_native_pdf_page(page, image, {}))
        page.close()
        pdf.close()

    def test_real_native_page_preserves_each_character_once(self):
        source = PRIVATE_PDF
        if not source.exists():
            self.skipTest("User-supplied PDF unavailable")
        pdf = pdfium.PdfDocument(str(source))
        page = pdf[3]
        textpage = page.get_textpage()
        expected = Counter("".join(textpage.get_text_range().split()))
        textpage.close()
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as folder:
            image_path = Path(folder) / "source.png"
            page.render(scale=150 / 72).to_pil().save(image_path)
            result = recognize_native_pdf_page(page, image_path, {})
            glyphs = _glyphs(page, 1240, 1753)
            # This PDF uses ~100pt text objects scaled by a 0.12 CTM.
            # The physical header is ~25px; ignoring the CTM gives ~208px.
            header_glyph = next(g for g in glyphs if g["text"] == "서" and 600 < g["x"] < 650 and 490 < g["y"] < 540)
            self.assertAlmostEqual(header_glyph["font_size"], header_glyph["height"], delta=1)
            observed = []
            for block in result["blocks"]:
                if block["type"] == "text":
                    observed.append(block["text"])
                elif block["type"] == "table":
                    observed.extend("".join(row) for row in block["rows"])
            self.assertEqual(Counter("".join("".join(observed).split())), expected)
            cells = [cell["text"] for block in result["blocks"] if block["type"] == "table"
                     for cell in block["cells"]]
            self.assertIn("서 울 시*", cells)
            self.assertIn("자치구**", cells)
            self.assertEqual(result["engine"], "native")
            self.assertFalse(result["measured"])
            self.assertTrue((Path(folder) / "source-native-background.png").exists())
            self.assertTrue(any(block["type"] == "text" and block.get("source") == "native-pdf-text" for block in result["blocks"]))
        page.close()
        pdf.close()


if __name__ == "__main__":
    unittest.main()
