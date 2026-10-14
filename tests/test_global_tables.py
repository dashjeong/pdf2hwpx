import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from converter.global_tables import compose_source_crop, recover_global_tables
from converter.large_raster import tile_plan
from converter.ocr import _find_tables
import cv2


def text(value, x, y, confidence=.99):
    return {'type': 'text', 'text': value, 'x': x, 'y': y, 'width': 40, 'height': 25, 'confidence': confidence}


class GlobalGridTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'build'
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self):
        source = Image.new('RGB', (600, 600), 'white')
        draw = ImageDraw.Draw(source)
        font = ImageFont.truetype('C:/Windows/Fonts/malgun.ttf', 28)
        for x in (350, 430, 510, 590):
            draw.line((x, 350, x, 590), fill='black', width=3)
        for y in (350, 430, 510, 590):
            draw.line((350, y, 590, y), fill='black', width=3)
        for row in range(3):
            for col in range(3):
                draw.text((365 + col * 80, 365 + row * 80), str(71 + row * 3 + col), font=font, fill='black')
        for row in range(6):
            for col in range(12):
                color = ('#156dac', '#ef9b33', '#86bb5d')[(row + col) % 3]
                draw.rectangle((350 + col * 20, 190 + row * 20, 370 + col * 20, 210 + row * 20), fill=color)
        preview = self.folder / 'preview.png'
        source.save(preview)
        records = []
        for index, tile in enumerate(tile_plan(600, 600, 400 * 400)):
            path = self.folder / f'tile-{index}.png'
            source.crop((tile['x'], tile['y'], tile['x'] + tile['width'], tile['y'] + tile['height'])).save(path)
            tile['path'] = str(path)
            records.append({'tile': tile, 'result': {}})
        page = {'width': 600, 'height': 600, 'image_url': f'/api/assets/{self.folder.name}/preview.png'}
        blocks = [{'type': 'table', 'x': 350, 'y': 350, 'width': 80, 'height': 160,
                   'rows': [['71'], ['74']], 'cells': []},
                  {'type': 'table', 'x': 450, 'y': 190, 'width': 140, 'height': 120,
                   'rows': [[''] * 7 for _ in range(6)], 'cells': []},
                  text('72', 445, 370), text('/3', 525, 370), text('75', 445, 450),
                  text('/6', 525, 450), text('77', 365, 530), text('78', 445, 530), text('79', 525, 530)]
        return source, page, records, blocks

    def test_complete_global_grid_overrides_fragments_and_retains_exact_source_colors(self):
        source, page, records, blocks = self.fixture()
        original = copy.deepcopy(blocks)
        rereads = [{'blocks': [text(str(value), 7, 7)]} for value in range(71, 80)]
        warnings = []
        with patch('converter.ocr.recognize_page', side_effect=rereads) as recognize:
            output = recover_global_tables(page, records, blocks, 'local', warnings)
        tables = [block for block in output if block['type'] == 'table']
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['rows'], [['71', '72', '73'], ['74', '75', '76'], ['77', '78', '79']])
        self.assertEqual(len(tables[0]['cells']), 9)
        self.assertEqual(recognize.call_count, 9)
        self.assertEqual(blocks, original)
        self.assertEqual(next(c for c in tables[0]['cells'] if c['row'] == 0 and c['col'] == 2)['seam_alternatives'][0]['text'], '/3')
        images = [block for block in output if block.get('source_graphic_preserved')]
        self.assertEqual(len(images), 1)
        block = images[0]
        with Image.open(self.folder / Path(block['image_url']).name) as actual:
            pixels = np.asarray(actual.crop((350 - block['x'], 190 - block['y'], 591 - block['x'], 311 - block['y'])))
        self.assertTrue(np.array_equal(pixels, np.asarray(source.crop((350, 190, 591, 311)))))
        self.assertTrue(warnings)
        self.assertFalse(list(self.folder.glob('.global-table-*-cell-*.png')))
        source.close()

    def test_failed_cell_read_keeps_existing_editable_observations(self):
        source, page, records, blocks = self.fixture()
        rereads = [{'blocks': [text(str(value), 7, 7)]} for value in range(71, 79)] + [{'blocks': []}]
        warnings = []
        with patch('converter.ocr.recognize_page', side_effect=rereads):
            output = recover_global_tables(page, records, blocks, 'local', warnings)
        self.assertFalse(any(block.get('global_grid_recovered') for block in output))
        self.assertTrue(any(block.get('text') == '/3' for block in output))
        self.assertTrue(any('유지' in warning for warning in warnings))
        source.close()

    def test_selected_provider_is_not_replaced_by_local_ocr(self):
        source, page, records, blocks = self.fixture()
        with patch('converter.ocr.recognize_page') as recognize:
            output = recover_global_tables(page, records, blocks, 'openai', [])
        recognize.assert_not_called()
        self.assertTrue(any(block.get('text') == '/3' for block in output))
        source.close()

    def test_confirmed_provider_numeric_conflict_is_retained_and_warned(self):
        source, page, records, _ = self.fixture()
        blocks = [text(str(71 + row * 3 + col), 365 + col * 80, 370 + row * 80)
                  for row in range(3) for col in range(3)]
        blocks.append(text('1284', 365, 370))
        warnings = []
        with patch('converter.ocr.recognize_page') as recognize:
            output = recover_global_tables(page, records, blocks, 'gemini', warnings)
        recognize.assert_not_called()
        self.assertFalse(any(block.get('global_grid_recovered') for block in output))
        self.assertTrue(any(block.get('text') == '71' for block in output))
        self.assertTrue(any(block.get('text') == '1284' for block in output))
        self.assertTrue(warnings)
        source.close()

    def test_blank_gray_form_and_merged_header_remain_editable(self):
        source = Image.new('RGB', (600, 400), 'white')
        draw = ImageDraw.Draw(source)
        draw.rectangle((30, 30, 570, 270), fill=(185, 185, 185))
        for y in (30, 110, 190, 270):
            draw.line((30, y, 570, y), fill='black', width=3)
        for x in (30, 570):
            draw.line((x, 30, x, 270), fill='black', width=3)
        for x in (210, 390):
            draw.line((x, 110, x, 270), fill='black', width=3)
        path = self.folder / 'gray.png'
        source.save(path)
        records = [{'tile': {'path': str(path), 'x': 0, 'y': 0, 'width': 600, 'height': 400, 'core': [0, 0, 600, 400]}, 'result': {}}]
        page = {'width': 600, 'height': 400, 'image_url': f'/api/assets/{self.folder.name}/gray.png'}
        with patch('converter.ocr.recognize_page') as recognize:
            output = recover_global_tables(page, records, [], 'local', [])
        recognize.assert_not_called()
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]['type'], 'table')
        self.assertEqual(output[0]['rows'], [['', '', ''], ['', '', ''], ['', '', '']])
        self.assertEqual(next(cell for cell in output[0]['cells'] if cell['row'] == 0)['colspan'], 3)
        self.assertFalse(any(block.get('source_graphic_preserved') for block in output))
        source.close()

    def test_preview_permissions_pixel_cap_and_missing_source_retain_original(self):
        source, page, records, blocks = self.fixture()
        with patch('converter.ocr._find_tables') as detect:
            bad = {**page, 'image_url': '/api/assets/other-job/preview.png'}
            self.assertEqual(recover_global_tables(bad, records, blocks, 'local', []), blocks)
            detect.assert_not_called()
        oversized = Image.new('RGB', (2001, 2001), 'white')
        oversized.save(self.folder / 'oversized.png')
        oversized.close()
        with patch('converter.ocr._find_tables') as detect:
            bad = {**page, 'image_url': f'/api/assets/{self.folder.name}/oversized.png'}
            self.assertEqual(recover_global_tables(bad, records, blocks, 'local', []), blocks)
            detect.assert_not_called()
        self.assertIsNone(compose_source_crop(records, (0, 0, 600, 600)))
        missing = copy.deepcopy(records)
        missing[0]['tile']['path'] = str(self.folder / 'missing.png')
        self.assertIsNone(compose_source_crop(missing, (0, 0, 80, 80)))
        source.close()


if __name__ == '__main__':
    unittest.main()

