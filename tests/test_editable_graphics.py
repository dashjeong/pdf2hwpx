"""Editable form shapes retain geometry while real pictures stay local."""
from pathlib import Path
from tempfile import TemporaryDirectory
import ctypes
import unittest
from unittest.mock import patch

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from converter.editable_graphics import (extract_editable_graphics, native_pdf_shapes,
                                        sample_table_styles, apply_native_table_styles,
                                        upgrade_legacy_page, _shape, _dominant_color)
from converter.ocr import _remap_source_blocks

ROOT = Path(__file__).resolve().parents[1]


class EditableGraphicsTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'build').mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=ROOT / 'build')
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'page.png'

    def test_dotted_text_frame_does_not_leave_a_picture_covering_the_words(self):
        image = np.full((300, 600, 3), 255, np.uint8)
        for x in range(30, 571, 3):
            image[40, x] = image[240, x] = 90
        for y in range(40, 241, 3):
            image[y, 30] = image[y, 570] = 90
        image[80:95, 60:250] = 0
        image[120:135, 60:350] = 0
        texts = [{'x': 60, 'y': y, 'width': width, 'height': 15}
                 for y, width in [(80, 190), (120, 290)]]
        # A real picture outside the frame must remain a picture.
        image[255:285, 100:140] = np.random.default_rng(123).integers(0, 210, (30, 40, 3), dtype=np.uint8)
        original = image.copy()
        blocks = extract_editable_graphics(image, texts, [], self.source)
        dotted = [b for b in blocks if b['type'] == 'shape' and b.get('stroke_style') == 'DOT']
        self.assertEqual(len(dotted), 4)
        self.assertTrue(all(b['shape'] == 'line' for b in dotted))
        pictures = [b for b in blocks if b['type'] == 'image']
        self.assertTrue(pictures)
        self.assertFalse(any(b['width'] > 400 and b['height'] > 100 for b in pictures))
        self.assertTrue(np.array_equal(image, original))

    def test_scanned_form_rules_are_shapes_not_page_background(self):
        image = np.full((300, 400, 3), 255, np.uint8)
        cv2.rectangle(image, (20, 30), (370, 260), (0, 0, 0), 2)
        cv2.line(image, (20, 100), (370, 100), (0, 0, 0), 2)
        before = image.copy()
        blocks = extract_editable_graphics(image, [], [], self.source)
        self.assertGreaterEqual(sum(b['type'] == 'shape' for b in blocks), 5)
        self.assertFalse(any(b.get('role') == 'graphics-background' for b in blocks))
        self.assertFalse(any(b['type'] == 'image' and b['width'] > 350 and b['height'] > 220 for b in blocks))
        np.testing.assert_array_equal(image, before)

    def test_colored_form_bar_is_an_editable_fill(self):
        image = np.full((160, 300, 3), 255, np.uint8)
        image[30:60, 20:280] = (220, 120, 30)
        blocks = extract_editable_graphics(image, [], [], self.source)
        bars = [b for b in blocks if b['type'] == 'shape' and b.get('fill_color') == '#1e78dc']
        self.assertTrue(bars)
        self.assertTrue(bars[0]['closed'])
        self.assertIsNone(bars[0]['stroke_color'])
        self.assertFalse(any(b['type'] == 'image' for b in blocks))

    def test_actual_table_keeps_cell_fill_and_border_style(self):
        image = np.full((150, 220, 3), 255, np.uint8)
        image[20:70, 20:200] = (255, 150, 30)
        cv2.rectangle(image, (20, 20), (200, 120), (0, 0, 0), 3)
        cv2.line(image, (20, 70), (200, 70), (0, 0, 0), 3)
        table = {'type': 'table', 'x': 20, 'y': 20, 'width': 180, 'height': 100,
                 'rows': [['제목'], ['내용']], 'cells': [
                     {'row': 0, 'col': 0, 'x': 20, 'y': 20, 'width': 180, 'height': 50},
                     {'row': 1, 'col': 0, 'x': 20, 'y': 70, 'width': 180, 'height': 50}]}
        sample_table_styles(image, [table])
        self.assertEqual(table['cells'][0]['fill_color'], '#1e96ff')
        self.assertEqual(table['cells'][1]['fill_color'], '#ffffff')
        self.assertEqual(table['cells'][0]['border_color'], '#000000')
        self.assertGreaterEqual(table['cells'][0]['border_width'], 3)

    def test_native_pdf_rect_geometry_fill_and_stroke_are_editable(self):
        pdf = pdfium.PdfDocument.new()
        page = pdf.new_page(200, 200)
        self.addCleanup(pdf.close)
        self.addCleanup(page.close)
        obj = pdfium.raw.FPDFPageObj_CreateNewRect(10, 20, 80, 40)
        pdfium.raw.FPDFPageObj_SetFillColor(obj, 30, 120, 220, 255)
        pdfium.raw.FPDFPageObj_SetStrokeColor(obj, 10, 20, 30, 255)
        pdfium.raw.FPDFPageObj_SetStrokeWidth(obj, 2)
        pdfium.raw.FPDFPath_SetDrawMode(obj, pdfium.raw.FPDF_FILLMODE_WINDING, True)
        pdfium.raw.FPDFPage_InsertObject(page, obj)
        shapes, protected = native_pdf_shapes(page, 400, 400, [])
        self.assertEqual(len(shapes), 1)
        shape = shapes[0]
        self.assertEqual(shape['shape'], 'rect')
        self.assertAlmostEqual(shape['x'], 20)
        self.assertAlmostEqual(shape['y'], 280)
        self.assertAlmostEqual(shape['width'], 160)
        self.assertEqual(shape['fill_color'], '#1e78dc')
        self.assertEqual(shape['stroke_color'], '#0a141e')
        self.assertAlmostEqual(shape['stroke_width'], 4)
        self.assertFalse(protected)

    def test_rounded_native_path_is_editable_polyline(self):
        pdf = pdfium.PdfDocument.new()
        page = pdf.new_page(200, 200)
        self.addCleanup(pdf.close)
        self.addCleanup(page.close)
        obj = pdfium.raw.FPDFPageObj_CreateNewPath(20, 40)
        pdfium.raw.FPDFPath_LineTo(obj, 80, 40)
        pdfium.raw.FPDFPath_BezierTo(obj, 100, 40, 100, 60, 80, 60)
        pdfium.raw.FPDFPath_LineTo(obj, 20, 60)
        pdfium.raw.FPDFPath_Close(obj)
        pdfium.raw.FPDFPageObj_SetStrokeColor(obj, 0, 0, 0, 255)
        pdfium.raw.FPDFPath_SetDrawMode(obj, 0, True)
        pdfium.raw.FPDFPage_InsertObject(page, obj)
        shapes, _ = native_pdf_shapes(page, 400, 400, [])
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0]['shape'], 'polyline')
        self.assertTrue(shapes[0]['closed'])
        self.assertGreater(len(shapes[0]['points']), 10)

    def test_affine_mapping_preserves_relative_points_and_stroke(self):
        image = np.full((300, 400, 3), 255, np.uint8)
        shape = _shape([[10, 20], [70, 20]], stroke_width=2)
        inverse = np.array([[2., 0., 5.], [0., 2., 7.]])
        blocks = _remap_source_blocks([shape], image, inverse, self.source)
        line = next(b for b in blocks if b['type'] == 'shape')
        self.assertEqual(line['x'], 25)
        self.assertEqual(line['y'], 47)
        self.assertEqual(line['width'], 120)
        self.assertEqual(line['height'], 0)
        self.assertEqual(line['points'], [[0., 0.], [120., 0.]])
        self.assertEqual(line['stroke_width'], 4)

    def test_real_picture_is_a_local_image_and_form_rules_are_shapes(self):
        image = np.full((300, 400, 3), 255, np.uint8)
        cv2.line(image, (20, 30), (380, 30), (0, 0, 0), 2)
        for y in range(80, 180):
            for x in range(90, 190):
                image[y, x] = (x % 200, y % 180, (x + y) % 220)
        blocks = extract_editable_graphics(image, [], [], self.source)
        self.assertTrue(any(b['type'] == 'shape' for b in blocks))
        pictures = [b for b in blocks if b['type'] == 'image']
        self.assertTrue(pictures)
        self.assertTrue(all(b['width'] < 200 and b['height'] < 200 for b in pictures))
        self.assertTrue(all(b.get('role') == 'picture' for b in pictures))

    def test_searchable_scan_native_layer_keeps_raster_form_editable(self):
        from converter.native_pdf import recognize_native_pdf_page
        image = np.full((300, 400, 3), 255, np.uint8)
        cv2.rectangle(image, (20, 30), (380, 270), (0, 0, 0), 2)
        cv2.putText(image, 'SCAN 12345', (50, 80), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 1)
        pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        pil.save(self.source)
        pdf = pdfium.PdfDocument.new()
        page = pdf.new_page(400, 300)
        bitmap = pdfium.PdfBitmap.from_pil(pil)
        self.addCleanup(pdf.close)
        self.addCleanup(bitmap.close)
        self.addCleanup(page.close)
        image_object = pdfium.PdfImage.new(pdf)
        image_object.set_bitmap(bitmap)
        image_object.set_matrix(pdfium.PdfMatrix(400, 0, 0, 300, 0, 0))
        page.insert_obj(image_object)
        font = pdfium.raw.FPDFText_LoadStandardFont(pdf, b'Helvetica')
        text = pdfium.raw.FPDFPageObj_CreateTextObj(pdf, font, 16)
        encoded = 'SCAN 12345'.encode('utf-16-le') + b'\0\0'
        characters = (ctypes.c_ushort * (len(encoded) // 2)).from_buffer_copy(encoded)
        pdfium.raw.FPDFText_SetText(text, characters)
        pdfium.raw.FPDFPageObj_SetFillColor(text, 0, 0, 0, 0)
        pdfium.raw.FPDFPageObj_Transform(text, 1, 0, 0, 1, 50, 220)
        pdfium.raw.FPDFPage_InsertObject(page, text)
        _, protected = native_pdf_shapes(page, 400, 300, [])
        self.assertFalse(protected, 'The page-sized scan must not bypass form extraction as a photograph')
        result = recognize_native_pdf_page(page, self.source, {})
        self.assertIsNotNone(result)
        tables = [block for block in result['blocks'] if block['type'] == 'table']
        self.assertEqual(len(tables), 1)
        self.assertIn('SCAN', tables[0]['rows'][0][0])
        self.assertFalse(any(b['type'] == 'shape' for b in result['blocks']))
        self.assertFalse(any(b.get('role') == 'graphics-background' for b in result['blocks']))
        self.assertFalse(any(b['type'] == 'image' and b['width'] > 350 and b['height'] > 240 for b in result['blocks']))

    def test_legacy_migration_preserves_user_text_table_and_style_edits(self):
        job = self.source.parent / 'legacy-job'
        job.mkdir()
        original = Image.new('RGB', (400, 300), 'white')
        bgr = cv2.cvtColor(np.asarray(original), cv2.COLOR_RGB2BGR)
        cv2.rectangle(bgr, (20, 30), (380, 270), (0, 0, 0), 2)
        Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).save(job / 'background.png')
        page = {'width': 400, 'height': 300, 'blocks': [
            {'type': 'image', 'role': 'graphics-background', 'x': 0, 'y': 0, 'width': 400, 'height': 300,
             'image_url': '/api/assets/legacy-job/background.png'},
            {'type': 'text', 'x': 40, 'y': 50, 'width': 100, 'height': 20, 'text': '편집한 글자 123'},
            {'type': 'table', 'x': 80, 'y': 120, 'width': 150, 'height': 50, 'rows': [['편집한 표']],
             'fill_color': '#00ff00', 'cells': [{'row': 0, 'col': 0, 'x': 80, 'y': 120, 'width': 150, 'height': 50,
                 'fill_color': '#ff0000', 'border_color': '#123456', 'border_width': 5}]}]}
        result = upgrade_legacy_page(page, self.source.parent, 'legacy-job')
        self.assertTrue(any(b.get('text') == '편집한 글자 123' for b in result['blocks']))
        table = next(b for b in result['blocks'] if b['type'] == 'table')
        self.assertEqual(table['rows'], [['편집한 표']])
        self.assertEqual(table['fill_color'], '#00ff00')
        self.assertEqual(table['cells'][0]['fill_color'], '#ff0000')
        self.assertEqual(table['cells'][0]['border_width'], 5)
        self.assertTrue(any(b['type'] == 'shape' for b in result['blocks']))
        self.assertFalse(any(b.get('role') == 'graphics-background' for b in result['blocks']))

    def test_legacy_large_image_is_rejected_before_rgb_decode(self):
        job = self.source.parent / 'legacy-job'
        job.mkdir()
        (job / 'background.png').write_bytes(b'header-only-test')
        page = {'width': 16_000_001, 'height': 1, 'blocks': [{'type': 'image', 'role': 'graphics-background',
            'width': 16_000_001, 'height': 1, 'image_url': '/api/assets/legacy-job/background.png'}]}
        class HeaderOnly:
            width, height = 16_000_001, 1
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def convert(self, *_): raise AssertionError('RGB decoding must not happen')
        with patch('converter.editable_graphics.Image.open', return_value=HeaderOnly()):
            with self.assertRaisesRegex(ValueError, '다시 변환'):
                upgrade_legacy_page(page, self.source.parent, 'legacy-job')

    def test_legacy_half_size_preview_maps_graphics_back_to_page_geometry(self):
        job = self.source.parent / 'legacy-job'
        job.mkdir()
        preview = np.full((150, 200, 3), 255, np.uint8)
        cv2.line(preview, (20, 20), (180, 20), (0, 0, 0), 2)
        Image.fromarray(preview).save(job / 'preview.png')
        Image.new('RGB', (100, 20), 'white').save(job / 'crop.png')
        page = {'width': 400, 'height': 300, 'image_url': '/api/assets/legacy-job/preview.png', 'blocks': [
            {'type': 'image', 'x': 30, 'y': 40, 'width': 200, 'height': 40, 'role': 'graphics-background',
             'image_url': '/api/assets/legacy-job/crop.png'},
            {'type': 'text', 'x': 80, 'y': 90, 'width': 150, 'height': 30, 'text': '편집 유지'}]}
        result = upgrade_legacy_page(page, self.source.parent, 'legacy-job')
        lines = [b for b in result['blocks'] if b['type'] == 'shape']
        self.assertTrue(lines)
        line = max(lines, key=lambda b: b['width'])
        self.assertAlmostEqual(line['y'], 40, delta=2)
        self.assertAlmostEqual(line['x'], 40, delta=4)
        self.assertAlmostEqual(line['width'], 320, delta=6)
        text = next(b for b in result['blocks'] if b['type'] == 'text')
        self.assertEqual((text['x'], text['y'], text['text']), (80, 90, '편집 유지'))

    def test_diagonal_form_rules_do_not_turn_into_a_whole_page_picture(self):
        image = np.full((400, 300, 3), 255, np.uint8)
        cv2.line(image, (0, 0), (299, 399), (0, 0, 0), 3)
        cv2.line(image, (299, 0), (0, 399), (0, 0, 0), 3)
        blocks = extract_editable_graphics(image, [], [], self.source)
        diagonals = [b for b in blocks if b['type'] == 'shape' and b['width'] > 200 and b['height'] > 300]
        self.assertGreaterEqual(len(diagonals), 2)
        self.assertFalse(any(b['type'] == 'image' and b['width'] >= 290 and b['height'] >= 390 for b in blocks))

    def test_color_histogram_samples_at_most_64k_pixels(self):
        image = np.full((1200, 1800, 3), (30, 60, 90), np.uint8)
        real = np.bincount
        counts = []
        def bounded(values, **kwargs):
            counts.append(len(values))
            return real(values, **kwargs)
        with patch('converter.editable_graphics.np.bincount', side_effect=bounded):
            self.assertEqual(_dominant_color(image), '#5a3c1e')
        self.assertTrue(counts)
        self.assertLessEqual(max(counts), 65536)

    def test_native_table_border_colors_are_recorded_per_edge(self):
        pdf = pdfium.PdfDocument.new()
        page = pdf.new_page(200, 200)
        self.addCleanup(pdf.close)
        self.addCleanup(page.close)
        # Separate horizontal/vertical source rules use distinct colors.
        for a, b, color in [((10, 40), (90, 40), (255, 0, 0)),
                            ((10, 80), (90, 80), (0, 0, 255)),
                            ((10, 40), (10, 80), (0, 0, 0)),
                            ((90, 40), (90, 80), (0, 255, 0))]:
            obj = pdfium.raw.FPDFPageObj_CreateNewPath(*a)
            pdfium.raw.FPDFPath_LineTo(obj, *b)
            pdfium.raw.FPDFPageObj_SetStrokeColor(obj, *color, 255)
            pdfium.raw.FPDFPageObj_SetStrokeWidth(obj, 1)
            pdfium.raw.FPDFPath_SetDrawMode(obj, 0, True)
            pdfium.raw.FPDFPage_InsertObject(page, obj)
        table = {'x': 20, 'y': 240, 'width': 160, 'height': 80, 'cells': [
            {'x': 20, 'y': 240, 'width': 160, 'height': 80, 'border_color': '#000000'}]}
        apply_native_table_styles(page, 400, 400, [table])
        edges = table['cells'][0]['borders']
        self.assertEqual(edges['top']['color'], '#0000ff')
        self.assertEqual(edges['bottom']['color'], '#ff0000')
        self.assertEqual(edges['left']['color'], '#000000')
        self.assertEqual(edges['right']['color'], '#00ff00')

    def test_dotted_style_survives_small_scan_ocr_upscaling(self):
        from converter.editable_graphics import raster_form_shapes
        image = np.full((140, 800, 3), 255, np.uint8)
        for x in range(30, 770, 3):
            image[35, x] = 0
        for scale in (1, 2.8):
            with self.subTest(scale=scale):
                enlarged = cv2.resize(image, (round(800 * scale), round(140 * scale)), interpolation=cv2.INTER_CUBIC)
                rules = raster_form_shapes(enlarged, [], [])
                self.assertEqual(len(rules), 1)
                self.assertEqual(rules[0]['stroke_style'], 'DOT')
        dashed = np.full_like(image, 255)
        for x in range(30, 770, 8):
            dashed[35, x:x + 4] = 0
        rules = raster_form_shapes(dashed, [], [])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]['stroke_style'], 'DASH')

    def test_genuine_full_page_photo_remains_an_image(self):
        image = np.random.default_rng(7).integers(0, 230, (120, 160, 3), dtype=np.uint8)
        blocks = extract_editable_graphics(image, [], [], self.source)
        self.assertTrue(any(b['type'] == 'image' and b.get('source') == 'full-page-photo' for b in blocks))
        self.assertFalse(any(b.get('role') == 'graphics-background' for b in blocks))

    def test_residual_picture_alpha_does_not_cover_editable_vector(self):
        image = np.full((180, 220, 3), 255, np.uint8)
        image[40:140, 50:170] = np.random.default_rng(8).integers(0, 220, (100, 120, 3), dtype=np.uint8)
        cv2.line(image, (50, 90), (130, 90), (0, 0, 0), 3)
        line = _shape([[50, 90], [130, 90]], stroke_width=3)
        blocks = extract_editable_graphics(image, [], [], self.source, native_shapes=[line])
        picture = next(b for b in blocks if b['type'] == 'image' and b['y'] <= 90 <= b['y'] + b['height'])
        path = self.source.parent / picture['image_url'].rsplit('/', 1)[-1]
        with Image.open(path) as png:
            self.assertEqual(png.mode, 'RGBA')
            self.assertEqual(png.getpixel((100 - picture['x'], 90 - picture['y']))[3], 0)

    def test_native_filled_form_does_not_erase_overlay_artwork(self):
        image = np.full((200, 300, 3), 255, np.uint8)
        image[20:180, 20:280] = (220, 120, 30)
        cv2.circle(image, (90, 85), 15, (0, 0, 255), -1)
        cv2.circle(image, (90, 85), 7, (220, 120, 30), -1)
        fill = _shape([[20, 20], [279, 20], [279, 179], [20, 179]], closed=True,
                      stroke=None, fill='#1e78dc', source='native-pdf-vector')
        blocks = extract_editable_graphics(image, [], [], self.source, native_shapes=[fill])
        pictures = [b for b in blocks if b['type'] == 'image']
        self.assertTrue(pictures)
        red_pixels = 0
        for picture in pictures:
            with Image.open(self.source.parent / picture['image_url'].rsplit('/', 1)[-1]) as png:
                pixels = np.asarray(png)
                red_pixels += int(np.count_nonzero((pixels[:, :, 0] == 255) & (pixels[:, :, 1] == 0)
                                                   & (pixels[:, :, 2] == 0) & (pixels[:, :, 3] == 255)))
        self.assertEqual(red_pixels, int(np.count_nonzero(np.all(image == (0, 0, 255), axis=2))))

    def test_white_artwork_on_black_form_fill_is_preserved(self):
        image = np.full((200, 300, 3), 255, np.uint8)
        image[20:180, 20:280] = (0, 0, 0)
        cv2.circle(image, (90, 85), 15, (255, 255, 255), -1)
        cv2.circle(image, (90, 85), 7, (0, 0, 0), -1)
        fill = _shape([[20, 20], [279, 20], [279, 179], [20, 179]], closed=True,
                      stroke=None, fill='#000000', source='native-pdf-vector')
        blocks = extract_editable_graphics(image, [], [], self.source, native_shapes=[fill])
        pictures = [b for b in blocks if b['type'] == 'image']
        self.assertTrue(pictures)
        pixels_preserved = 0
        for picture in pictures:
            with Image.open(self.source.parent / picture['image_url'].rsplit('/', 1)[-1]) as png:
                pixels = np.asarray(png)
                pixels_preserved += int(np.count_nonzero(np.all(pixels[:, :, :3] == 255, axis=2) & (pixels[:, :, 3] == 255)))
        expected = int(np.count_nonzero(np.all(image[60:110, 65:115] == 255, axis=2)))
        self.assertEqual(pixels_preserved, expected)


if __name__ == '__main__':
    unittest.main()
