from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageDraw

from converter import ocr


class OcrLayoutTests(unittest.TestCase):
    def test_enlarged_table_border_widths_map_back_once_even_when_shared(self):
        shared = {'top': {'style': 'DOT', 'width': 3}, 'left': {'style': 'DASH', 'width': 6}}
        table = dict(type='table', x=20, y=20, width=200, height=100, rows=[['123']],
                     border_width=3, borders=shared, cells=[dict(row=0, col=0, x=20, y=20,
                     width=200, height=100, text='123', borders=shared, border_width=3)])
        restored = ocr._remap_source_blocks([table], np.full((100, 200, 3), 255, np.uint8),
                                            np.array([[.5, 0, 0], [0, .5, 0]]), Path('unused.png'))[0]
        for style in (restored, restored['cells'][0]):
            self.assertEqual(style['border_width'], 1.5)
            self.assertEqual(style['borders']['top'], {'style': 'DOT', 'width': 1.5})
            self.assertEqual(style['borders']['left'], {'style': 'DASH', 'width': 3})
        self.assertEqual(shared['top']['width'], 3)

    def test_closed_text_frame_becomes_one_real_table_without_boxing_outside_text(self):
        from io import BytesIO
        from zipfile import ZipFile
        import xml.etree.ElementTree as ET
        from converter.hwpx import NS, export_hwpx, export_html
        image = np.full((350, 800, 3), 255, np.uint8)
        for x in range(30, 771, 3):
            image[30, x] = image[260, x] = 0
        for y in range(30, 261, 3):
            image[y, 30] = image[y, 770] = 0
        entries = [[[[65, 85], [430, 85], [430, 112], [65, 112]], '표 안의 글자 34567', .99],
                   [[[65, 165], [430, 165], [430, 193], [65, 193]], '두 번째 문장', .99],
                   [[[40, 300], [430, 300], [430, 325], [40, 325]], '표 밖의 일반 본문', .99]]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'scan.png'
            with patch.object(ocr, '_local_engine') as factory:
                factory.return_value.return_value = (entries, None)
                blocks, _ = ocr._recognize_local(image, source, {})
            tables = [b for b in blocks if b['type'] == 'table']
            self.assertEqual(len(tables), 1)
            self.assertEqual(tables[0]['rows'], [['표 안의 글자 34567\n두 번째 문장']])
            self.assertEqual([b['text'] for b in blocks if b['type'] == 'text'], ['표 밖의 일반 본문'])
            self.assertFalse([b for b in blocks if b['type'] in {'image', 'shape'}])
            doc = {'id': 'frame-test', 'pages': [{'width': 800, 'height': 350, 'dpi': 150, 'blocks': blocks}]}
            with ZipFile(BytesIO(export_hwpx(doc, Path(directory)))) as package:
                section = ET.fromstring(package.read('Contents/section0.xml'))
                header = ET.fromstring(package.read('Contents/header.xml'))
                self.assertEqual(len(section.findall('.//hp:tbl', NS)), 1)
                cell = section.find('.//hp:tc', NS)
                self.assertEqual(cell.get('editable'), '1')
                self.assertIn('34567', ''.join(cell.itertext()))
                self.assertFalse(section.findall('.//hp:pic', NS))
                self.assertFalse(section.findall('.//hp:drawText', NS))
                border = next(b for b in header.findall('.//hh:borderFill', NS) if b.get('id') == cell.get('borderFillIDRef'))
                self.assertEqual(border.find('hh:topBorder', NS).get('type'), 'DOT')
            self.assertIn(b'px dotted ', export_html(doc, Path(directory)))

    def test_single_cell_requires_closed_borders_and_text_and_excludes_photo(self):
        image = np.full((300, 600, 3), 255, np.uint8)
        text = dict(type='text', text='본문 123', x=60, y=70, width=200, height=25)
        for y in (30, 250):
            cv2.line(image, (30, y), (550, y), (0, 0, 0), 1)
        self.assertEqual(ocr._find_text_frames(image, [text], []), [])
        for x in (30, 550):
            cv2.line(image, (x, 30), (x, 250), (0, 0, 0), 1)
        self.assertEqual(ocr._find_text_frames(image, [], []), [])
        tables = ocr._find_text_frames(image, [text], [])
        self.assertEqual(len(tables), 1)
        self.assertEqual(ocr._find_text_frames(image, [text], tables), [])
        image[130:220, 100:500] = np.random.default_rng(12).integers(0, 220, (90, 400, 3), dtype=np.uint8)
        self.assertEqual(ocr._find_text_frames(image, [text], []), [])

    def test_table_rule_mask_preserves_long_character_strokes(self):
        image = Image.new("RGB", (1000, 600), "white")
        draw = ImageDraw.Draw(image)
        for y in (100, 250, 400):
            draw.line((60, y, 940, y), fill="black", width=3)
        for x in (60, 300, 940):
            draw.line((x, 100, x, 400), fill="black", width=3)
        # These interior strokes are long enough for the morphology kernels,
        # but they are neither table boundaries nor validated separators.
        draw.line((100, 150, 100, 190), fill="black", width=3)
        draw.line((130, 150, 180, 150), fill="black", width=3)
        source = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        tables, mask = ocr._find_tables(source)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]["cells"]), 4)
        erased = cv2.dilate(mask, np.ones((3, 3), np.uint8)) > 0
        self.assertFalse(np.any(erased[148:193, 98:103]))
        self.assertFalse(np.any(erased[148:153, 128:183]))
        self.assertTrue(erased[175, 300])
        self.assertTrue(erased[250, 500])

    def test_every_cell_reocr_empty_result_preserves_original_numbers_and_geometry(self):
        for count in (299, 300, 301, 310):
            with self.subTest(cells=count):
                cells = [{'row': r, 'col': 0, 'rowspan': 1, 'colspan': 1, 'x': 0, 'y': r * 20,
                          'width': 100, 'height': 20, 'text': '', '_texts': [], '_scores': []} for r in range(count)]
                table = {'type': 'table', 'x': 0, 'y': 0, 'width': 100, 'height': count * 20,
                         'cells': cells, 'rows': [[''] for _ in range(count)]}
                image = np.zeros((count * 20, 100, 3), np.uint8)
                polygon = [[4, (count - 1) * 20 + 4], [80, (count - 1) * 20 + 4],
                           [80, (count - 1) * 20 + 14], [4, (count - 1) * 20 + 14]]
                original = [polygon, '금액 1,234 [7]', .99]
                with patch.object(ocr, '_find_tables', return_value=([table], np.zeros(image.shape[:2], np.uint8))), \
                     patch.object(ocr, '_local_engine') as factory:
                    factory.return_value.side_effect = [([original], None)] + [([], None)] * count
                    blocks, _ = ocr._recognize_local(image, Path('unused.png'), {'preserve_images': False})
                self.assertEqual(factory.return_value.call_count, count + 1)
                self.assertEqual(blocks[0]['rows'][-1][0], '금액 1,234 [7]')
                self.assertEqual(blocks[0]['cells'][-1]['confidence'], .99)
                self.assertEqual(blocks[0]['cells'][-1]['text_blocks'][0]['polygon'], polygon)
                self.assertEqual(original[0], polygon)

    def test_cell_reocr_exception_keeps_successful_whole_page_reading_and_continues(self):
        cells = [{'row': 0, 'col': c, 'rowspan': 1, 'colspan': 1, 'x': c * 100, 'y': 0,
                  'width': 100, 'height': 30, 'text': '', '_texts': [], '_scores': []} for c in range(2)]
        table = {'type': 'table', 'x': 0, 'y': 0, 'width': 200, 'height': 30, 'cells': cells, 'rows': [['', '']]}
        image = np.zeros((30, 200, 3), np.uint8)
        polygon = [[4, 4], [80, 4], [80, 20], [4, 20]]
        with patch.object(ocr, '_find_tables', return_value=([table], np.zeros((30, 200), np.uint8))), \
             patch.object(ocr, '_local_engine') as factory:
            factory.return_value.side_effect = [([[polygon, '1234', .98]], None), RuntimeError('cell failed'),
                                                ([[polygon, '5678', .97]], None)]
            blocks, warnings = ocr._recognize_local(image, Path('unused.png'), {'preserve_images': False})
        self.assertEqual(blocks[0]['rows'], [['1234', '5678']])
        self.assertEqual(factory.return_value.call_count, 3)
        self.assertTrue(any('재인식이 실패' in warning for warning in warnings))

    def test_merged_cell_empty_crop_keeps_original_text(self):
        cell = {'row': 0, 'col': 0, 'rowspan': 1, 'colspan': 2, 'x': 0, 'y': 0,
                'width': 100, 'height': 30, 'text': '', '_texts': [], '_scores': []}
        table = {'type': 'table', 'x': 0, 'y': 0, 'width': 100, 'height': 30, 'cells': [cell], 'rows': [['', '']]}
        image = np.full((30, 100, 3), 255, np.uint8)
        with patch.object(ocr, '_find_tables', return_value=([table], np.zeros((30, 100), np.uint8))), \
             patch.object(ocr, '_local_engine') as factory:
            factory.return_value.return_value = ([[[[4, 4], [80, 4], [80, 20], [4, 20]], '각주 7', .99]], None)
            blocks, _ = ocr._recognize_local(image, Path('unused.png'), {'preserve_images': False})
        self.assertEqual(blocks[0]['rows'], [['각주 7', '']])
        self.assertEqual(blocks[0]['cells'][0]['colspan'], 2)
        self.assertEqual(factory.return_value.call_count, 1)

    def test_table_line_reads_left_to_right_despite_y_jitter(self):
        cell = {"row": 0, "col": 0, "x": 0, "y": 0, "width": 120, "height": 120,
                "_texts": [], "_scores": []}
        table = {"cells": [cell], "rows": [[""]]}
        blocks = [{"x": 10, "y": 51, "width": 40, "height": 15, "text": "서울", "confidence": .9},
                  {"x": 60, "y": 50, "width": 15, "height": 15, "text": "시", "confidence": .8},
                  {"x": 10, "y": 82, "width": 35, "height": 15, "text": "금액", "confidence": .9},
                  {"x": 60, "y": 80, "width": 40, "height": 15, "text": "1,234", "confidence": .8}]
        outside = ocr._assign_table_text(blocks, [table])
        self.assertEqual(outside, [])
        self.assertEqual(table["rows"], [["서울 시\n금액 1,234"]])
        self.assertEqual(cell["text"], "서울 시\n금액 1,234")
        self.assertEqual(cell["confidence"], .85)
        self.assertEqual([b["y"] for b in blocks], [51, 50, 82, 80])

    def test_table_font_size_uses_observed_geometry_and_preserves_explicit_value(self):
        for explicit in (None, 48):
            with self.subTest(explicit=explicit):
                cells = [{"row": 0, "col": col, "x": col * 100, "y": 0, "width": 100, "height": 80,
                          "_texts": [], "_scores": []} for col in range(3)]
                table = {"cells": cells, "rows": [["", "", ""]]}
                if explicit is not None:
                    table["font_size"] = explicit
                texts = [{"x": col * 100 + 10, "y": 10, "width": 50, "height": size,
                          "font_size": size, "text": str(col), "confidence": .9}
                         for col, size in enumerate((24, 32, 40))]
                ocr._assign_table_text(texts, [table])
                self.assertEqual(table["font_size"], 32 if explicit is None else 48)
                self.assertEqual(table["rows"], [["0", "1", "2"]])

    def test_vertical_cell_keeps_top_to_bottom_latin_then_korean(self):
        cell = {"row": 0, "col": 0, "x": 0, "y": 0, "width": 100, "height": 200,
                "_texts": [], "_scores": []}
        table = {"cells": [cell], "rows": [[""]]}
        blocks = [{"x": 14, "y": 10, "width": 8, "height": 24, "text": "AI", "confidence": .9},
                  {"x": 10, "y": 12, "width": 12, "height": 120, "text": "디지털정책팀", "confidence": .8}]
        ocr._assign_table_text(blocks, [table])
        self.assertEqual(table["rows"], [["AI 디지털정책팀"]])
        self.assertEqual([b["x"] for b in blocks], [14, 10])

    def test_merged_header_and_unequal_columns(self):
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        xs, ys = [60, 260, 720], [90, 190, 290]
        for y in ys:
            draw.line((xs[0], y, xs[-1], y), fill="black", width=2)
        for x in (xs[0], xs[-1]):
            draw.line((x, ys[0], x, ys[-1]), fill="black", width=2)
        draw.line((xs[1], ys[1], xs[1], ys[-1]), fill="black", width=2)
        tables, _ = ocr._find_tables(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
        self.assertEqual(len(tables), 1)
        cells = tables[0]["cells"]
        self.assertEqual(len(cells), 3)
        header = next(cell for cell in cells if cell["row"] == 0)
        self.assertEqual((header["col"], header["colspan"], header["rowspan"]), (0, 2, 1))
        self.assertAlmostEqual(tables[0]["column_widths"][0], 200, delta=2)
        self.assertAlmostEqual(tables[0]["column_widths"][1], 460, delta=2)

    def test_partial_divider_does_not_erase_glyph_in_merged_header(self):
        image = Image.new("RGB", (1000, 600), "white")
        draw = ImageDraw.Draw(image)
        for y in (100, 250, 400):
            draw.line((60, y, 940, y), fill="black", width=3)
        for x in (60, 940):
            draw.line((x, 100, x, 400), fill="black", width=3)
        draw.line((300, 250, 300, 400), fill="black", width=3)
        # The same x position is a text stroke in the merged top row.
        draw.line((300, 150, 300, 190), fill="black", width=3)
        source = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        tables, mask = ocr._find_tables(source)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]["cells"]), 3)
        header = next(cell for cell in tables[0]["cells"] if cell["row"] == 0)
        self.assertEqual(header["colspan"], 2)
        erased = cv2.dilate(mask, np.ones((3, 3), np.uint8)) > 0
        self.assertFalse(np.any(erased[148:193, 298:303]))
        self.assertTrue(erased[320, 300])

    def test_tile_clipped_table_keeps_interior_text_strokes(self):
        image = Image.new("RGB", (1000, 600), "white")
        draw = ImageDraw.Draw(image)
        for y in (100, 250, 400):
            draw.line((60, y, 940, y), fill="black", width=3)
        for x in (60, 300, 940):
            draw.line((x, 100, x, 400), fill="black", width=3)
        draw.line((400, 150, 400, 190), fill="black", width=3)
        # Both original outside borders are clipped by a raster tile.
        tile = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)[:, 200:900]
        tables, mask = ocr._find_tables(tile)
        self.assertEqual(len(tables), 1)
        erased = cv2.dilate(mask, np.ones((3, 3), np.uint8)) > 0
        self.assertFalse(np.any(erased[148:193, 198:203]))
        self.assertTrue(erased[175, 100])

    def test_diagram_frame_is_not_a_table(self):
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((90, 80, 560, 380), fill=(222, 235, 251), outline=(45, 75, 110), width=3)
        draw.ellipse((150, 150, 300, 300), fill=(76, 134, 190))
        draw.polygon([(350, 150), (480, 220), (350, 300)], fill=(220, 120, 80))
        tables, _ = ocr._find_tables(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
        self.assertEqual(tables, [])

    def test_scan_tilt_corrected_for_table_detection(self):
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        for y in (100, 200, 300):
            draw.line((60, y, 720, y), fill="black", width=2)
        for x in (60, 260, 720):
            draw.line((x, 100, x, 300), fill="black", width=2)
        image = image.rotate(.7, fillcolor="white", resample=Image.Resampling.BICUBIC)
        source = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        untouched = source.copy()
        working, inverse, angle = ocr._deskew_for_ocr(source)
        self.assertIsNotNone(inverse)
        self.assertAlmostEqual(abs(angle), .7, delta=.12)
        tables, _ = ocr._find_tables(working)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]["cells"]), 4)
        self.assertTrue(np.array_equal(source, untouched))
        mapped, _ = ocr._source_box(tables[0], inverse, 800, 600)
        self.assertGreaterEqual(mapped["x"], 0)
        self.assertLessEqual(mapped["x"] + mapped["width"], 800)

    def test_open_outer_borders_form_editable_table(self):
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        for y in (90, 190, 290, 390):
            draw.line((60, y, 720, y), fill="black", width=2)
        draw.line((260, 90, 260, 390), fill="black", width=2)
        tables, _ = ocr._find_tables(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]["rows"]), 3)
        self.assertEqual(len(tables[0]["rows"][0]), 2)

    def test_scaled_graphics_are_local_and_do_not_restore_old_text(self):
        image = np.full((100, 100, 3), 255, np.uint8)
        image[20:30, 20:30] = 0
        image[60:85, 65:90] = np.random.default_rng(0).integers(0, 220, (25, 25, 3), dtype=np.uint8)
        blocks = [{"type": "text", "x": 40, "y": 40, "width": 20, "height": 20, "text": "edited", "font_size": 20},
                  {"type": "image", "x": 20, "y": 20, "width": 80, "height": 80, "role": "graphics-background"}]
        build = Path(__file__).resolve().parents[1] / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as folder:
            source = Path(folder) / "source.png"
            result = ocr._remap_source_blocks(blocks, image, np.array([[.5, 0, 0], [0, .5, 0]], np.float64), source)
            self.assertEqual(result[0]["font_size"], 10)
            self.assertEqual(result[0]["text"], "edited")
            self.assertFalse(any(block.get('role') == 'graphics-background' for block in result))
            pictures = [block for block in result if block['type'] == 'image']
            self.assertTrue(pictures)
            self.assertTrue(all(block['width'] < 35 and block['height'] < 35 for block in pictures))
            self.assertFalse(any(block['x'] <= 25 <= block['x'] + block['width'] and
                                 block['y'] <= 25 <= block['y'] + block['height'] for block in pictures))

    def test_vision_response_clipped_and_confidence_unmeasured(self):
        raw = {"blocks": [
            {"type": "text", "x": -10, "y": 20, "width": 130, "height": 25, "text": "금액 1,234원", "confidence": 0.99},
            {"type": "image", "x": 75, "y": 65, "width": 200, "height": 200},
            {"type": "text", "x": "invalid", "y": 2, "width": 5, "height": 6, "text": "bad"}]}
        response = {"choices": [{"message": {"content": json.dumps(raw)}}]}
        image = np.full((100, 120, 3), 255, np.uint8)
        build = Path(__file__).resolve().parents[1] / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as folder, patch.object(ocr, "_api_request", return_value=response):
            source = Path(folder) / "page.png"
            blocks, _ = ocr._recognize_api(image, source, "openai", {"api_key": "secret-example"})
            # A provider rectangle over a blank white area is not a picture.
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["x"], 0)
            self.assertEqual(blocks[0]["width"], 120)
            self.assertIsNone(blocks[0]["confidence"])
            self.assertFalse(any(block['type'] == 'image' for block in blocks))
            self.assertNotIn("secret-example", json.dumps(blocks))

    def test_vision_whole_form_image_is_recovered_as_a_native_single_cell_table(self):
        raw = {'blocks': [{'type': 'text', 'x': 50, 'y': 60, 'width': 140, 'height': 25, 'text': '공문 양식 123'},
                          {'type': 'image', 'x': 0, 'y': 0, 'width': 300, 'height': 200}]}
        response = {'choices': [{'message': {'content': json.dumps(raw)}}]}
        image = np.full((200, 300, 3), 255, np.uint8)
        cv2.rectangle(image, (20, 30), (280, 180), (0, 0, 0), 2)
        build = Path(__file__).resolve().parents[1] / 'build'
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as folder, patch.object(ocr, '_api_request', return_value=response):
            blocks, _ = ocr._recognize_api(image, Path(folder) / 'page.png', 'openai', {'api_key': 'secret-example'})
            tables = [block for block in blocks if block['type'] == 'table']
            self.assertEqual(len(tables), 1)
            self.assertEqual(tables[0]['rows'], [['공문 양식 123']])
            self.assertFalse(any(block['type'] == 'shape' for block in blocks))
            self.assertFalse(any(block.get('role') == 'graphics-background' for block in blocks))
            self.assertFalse(any(block['type'] == 'image' and block['width'] > 250 and block['height'] > 140 for block in blocks))

    def test_incomplete_vision_json_has_actionable_error(self):
        response = {"choices": [{"message": {"content": '{"blocks": ['}}]}
        image = np.full((100, 120, 3), 255, np.uint8)
        with patch.object(ocr, "_api_request", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "JSON"):
                ocr._recognize_api(image, Path("unused.png"), "openai", {"api_key": "secret-example"})


if __name__ == "__main__":
    unittest.main()
