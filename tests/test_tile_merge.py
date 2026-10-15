import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from converter.tile_merge import AMBIGUOUS, merge_tile_results


def line(text, x, y, width, height=20, confidence=.9):
    return {'type': 'text', 'text': text, 'x': x, 'y': y, 'width': width, 'height': height,
            'confidence': confidence, 'polygon': [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]}


def record(x, y, width, height, core, blocks, path='unused.png'):
    return {'tile': {'x': x, 'y': y, 'width': width, 'height': height, 'core': core, 'path': path},
            'result': {'blocks': blocks}}


def table(xs, ys, values, spans=None):
    cells = []
    spans = spans or {}
    covered = set()
    for row in range(len(ys) - 1):
        for col in range(len(xs) - 1):
            if (row, col) in covered:
                continue
            rowspan, colspan = spans.get((row, col), (1, 1))
            covered.update((r, c) for r in range(row, row + rowspan) for c in range(col, col + colspan))
            cells.append({'row': row, 'col': col, 'rowspan': rowspan, 'colspan': colspan,
                          'x': xs[col], 'y': ys[row], 'width': xs[col + colspan] - xs[col],
                          'height': ys[row + rowspan] - ys[row], 'text': values[row][col], 'confidence': .9})
    return {'type': 'table', 'x': xs[0], 'y': ys[0], 'width': xs[-1] - xs[0], 'height': ys[-1] - ys[0],
            'rows': copy.deepcopy(values), 'cells': cells,
            'column_widths': [b - a for a, b in zip(xs, xs[1:])], 'row_heights': [b - a for a, b in zip(ys, ys[1:])]}


class TileMergeTests(unittest.TestCase):
    def test_overlapping_tile_form_rules_are_one_editable_line(self):
        def shape(x, y, width, color='#000000'):
            return {'type': 'shape', 'shape': 'line', 'x': x, 'y': y, 'width': width,
                    'height': 0, 'points': [[0, 0], [width, 0]],
                    'stroke_color': color, 'stroke_width': 1}
        records = [record(0, 0, 300, 100, [0, 0, 250, 100], [shape(20, 30, 280)]),
                   record(200, 0, 300, 100, [250, 0, 500, 100],
                          [shape(0, 30, 250), shape(0, 36, 250)])]
        originals = copy.deepcopy(records)
        result = merge_tile_results({}, records, 'local')
        lines = [b for b in result['blocks'] if b['type'] == 'shape']
        self.assertEqual(len(lines), 2)
        merged = next(b for b in lines if b['y'] == 30)
        self.assertEqual((merged['x'], merged['width'], merged['height']), (20, 430, 0))
        self.assertEqual(merged['points'], [[0, 0], [430, 0]])
        self.assertEqual(records, originals)

    def test_audit_long_line_suffix_prefix_is_once_and_original_untouched(self):
        records = [record(0, 0, 336, 100, [0, 0, 272, 100], [line('LEFT OVERLAP', 100, 10, 236)]),
                   record(208, 0, 336, 100, [272, 0, 544, 100], [line('OVERLAP RIGHT', 0, 10, 292)])]
        originals = copy.deepcopy(records)
        result = merge_tile_results({}, records, 'local')
        self.assertEqual([b['text'] for b in result['blocks']], ['LEFT OVERLAP RIGHT'])
        self.assertEqual(result['blocks'][0]['polygon'], [[100, 10], [500, 10], [500, 30], [100, 30]])
        self.assertEqual(records, originals)

    def test_repeated_words_on_different_lines_and_tiny_number_remain(self):
        records = [record(0, 0, 336, 100, [0, 0, 272, 100], [line('REPEAT', 210, 10, 80), line('7', 270, 80, 3, 4)]),
                   record(208, 0, 336, 100, [272, 0, 544, 100], [line('REPEAT', 2, 40, 80), line('7', 62, 80, 3, 4)])]
        result = merge_tile_results({}, records, 'local')
        self.assertEqual([b['text'] for b in result['blocks']], ['REPEAT', 'REPEAT', '7'])

    def test_different_numbers_same_source_position_are_retained_and_warned(self):
        result = merge_tile_results({}, [record(0, 0, 120, 80, [0, 0, 60, 80], [line('1,234', 40, 10, 60)]),
                                         record(30, 0, 120, 80, [60, 0, 150, 80], [line('1,284', 10, 10, 60, confidence=.99)])], 'local')
        self.assertEqual([b['text'] for b in result['blocks']], ['1,234', '1,284'])
        self.assertIn(AMBIGUOUS, result['warnings'])

    def test_long_number_exact_spatial_suffix(self):
        result = merge_tile_results({}, [record(0, 0, 160, 80, [0, 0, 120, 80], [line('1234567890', 20, 10, 100)]),
                                         record(80, 0, 160, 80, [120, 0, 240, 80], [line('7890123456', 0, 10, 100)])], 'local')
        self.assertEqual([b['text'] for b in result['blocks']], ['1234567890123456'])

    def test_periodic_number_uses_physical_overlap_not_longest_text_match(self):
        records = [record(0, 0, 800, 100, [0, 0, 672, 100],
                          [line('123456789012345678901234567890', 80, 10, 720)]),
                   record(608, 0, 800, 100, [672, 0, 1408, 100],
                          [line('56789012345678901234567890', 48, 10, 624)])]
        result = merge_tile_results({}, records, 'local')
        self.assertEqual([b['text'] for b in result['blocks']], ['12345678901234567890123456789012345678901234567890'])

    def test_complete_cell_extends_synthesized_crop_edge_border(self):
        partial = table([20, 100, 160], [10, 50, 90], [['A', '12'], ['B', '34']])
        complete = table([20, 120], [10, 50, 90], [['12'], ['34']])
        records = [record(0, 0, 160, 100, [0, 0, 130, 100], [partial]),
                   record(80, 0, 160, 100, [130, 0, 240, 100], [complete])]
        result = merge_tile_results({}, records, 'local')
        self.assertEqual(len(result['blocks']), 1)
        self.assertEqual(result['blocks'][0]['rows'], [['A', '12'], ['B', '34']])
        self.assertEqual(result['blocks'][0]['column_widths'], [80, 100])

    def test_same_source_number_with_crop_height_variation_is_once(self):
        result = merge_tile_results({}, [record(0, 0, 200, 100, [0, 0, 150, 100], [line('279', 120, 10, 41, 32)]),
                                         record(100, 0, 200, 100, [150, 0, 300, 100], [line('279', 21, 11, 41, 17)])], 'local')
        self.assertEqual([block['text'] for block in result['blocks']], ['279'])

    def test_bounded_source_cell_recheck_corrobates_existing_number_and_keeps_alternative(self):
        build = Path(__file__).resolve().parents[1] / 'build'
        with tempfile.TemporaryDirectory(dir=build) as folder:
            folder = Path(folder)
            paths = [folder / 'left.png', folder / 'right.png']
            for path in paths:
                Image.new('RGB', (160, 100), 'white').save(path)
            left = table([20, 100, 150], [10, 50, 90], [['A', '1='], ['B', 'D']])
            left['cells'][1]['text_blocks'] = [line('1=', 110, 20, 25, confidence=.7)]
            right = table([0, 50], [10, 50, 90], [['130'], ['D']])
            right['cells'][0]['text_blocks'] = [line('130', 10, 20, 40, confidence=.99)]
            records = [record(0, 0, 160, 100, [0, 0, 130, 100], [left], str(paths[0])),
                       record(100, 0, 160, 100, [130, 0, 260, 100], [right], str(paths[1]))]
            reread = {'blocks': [line('130', 7, 7, 40, confidence=.99)]}
            with patch('converter.ocr.recognize_page', return_value=reread) as recognize:
                result = merge_tile_results({}, records, 'local')
                self.assertGreaterEqual(recognize.call_count, 1)
            cell = next(cell for cell in result['blocks'][0]['cells'] if cell['row'] == 0 and cell['col'] == 1)
            self.assertEqual(cell['text'], '130')
            self.assertTrue(cell['seam_reocr'])
            self.assertEqual(cell['seam_alternatives'][0]['text'], '1=')
            self.assertFalse(list(folder.glob('*seam-cell*.png')))

    def test_complete_observation_retains_confidence_and_polygon(self):
        result = merge_tile_results({}, [record(0, 0, 160, 80, [0, 0, 120, 80], [line('전체 문장 123', 30, 10, 120, confidence=.97)]),
                                         record(80, 0, 160, 80, [120, 0, 240, 80], [line('문장 123', 0, 10, 70, confidence=.8)])], 'local')
        self.assertEqual(len(result['blocks']), 1)
        self.assertEqual(result['blocks'][0]['text'], '전체 문장 123')
        self.assertEqual(result['blocks'][0]['confidence'], .97)
        self.assertEqual(result['blocks'][0]['polygon'][0], [30, 10])

    def test_same_repeated_word_without_position_agreement_is_not_deleted(self):
        result = merge_tile_results({}, [record(0, 0, 300, 80, [0, 0, 250, 80], [line('WORD WORD', 10, 10, 200)]),
                                         record(230, 0, 300, 80, [250, 0, 530, 80], [line('WORD NEXT', 0, 10, 200)])], 'local')
        self.assertEqual(len(result['blocks']), 2)

    def test_vertical_tile_overlap_line_and_polygon_offset(self):
        result = merge_tile_results({}, [record(0, 0, 100, 140, [0, 0, 100, 100], [line('123456', 10, 20, 12, 120)]),
                                         record(0, 80, 100, 140, [0, 100, 100, 220], [line('456789', 10, 0, 12, 120)])], 'local')
        self.assertEqual([b['text'] for b in result['blocks']], ['123456789'])
        self.assertEqual(result['blocks'][0]['height'], 180)

    def test_horizontal_table_shared_cell_and_merged_header(self):
        left = table([20, 100, 200], [10, 50, 90], [['제목', ''], ['1', '2']], {(0, 0): (1, 2)})
        right = table([0, 100, 180], [10, 50, 90], [['제목', ''], ['2', '3']], {(0, 0): (1, 2)})
        # Header fragments terminate at both crop borders; lower row gives
        # genuine global column boundaries. Its duplicated word is one header.
        left['cells'][0]['text_blocks'] = [line('제목', 110, 20, 40)]
        right['cells'][0]['text_blocks'] = [line('제목', 10, 20, 40)]
        records = [record(0, 0, 200, 100, [0, 0, 150, 100], [left]),
                   record(100, 0, 180, 100, [150, 0, 280, 100], [right])]
        result = merge_tile_results({}, records, 'local')
        self.assertEqual(len(result['blocks']), 1)
        joined = result['blocks'][0]
        self.assertEqual(joined['rows'], [['제목', '', ''], ['1', '2', '3']])
        self.assertEqual(joined['cells'][0]['colspan'], 3)
        self.assertEqual(joined['cells'][0]['text_blocks'][0]['polygon'][0], [110, 20])

    def test_vertical_table_shared_row_preserves_numbers_and_spans(self):
        top = table([10, 60, 110], [10, 50, 90], [['HEADER', ''], ['1', '2']], {(0, 0): (1, 2)})
        bottom = table([10, 60, 110], [0, 40, 80], [['1', '2'], ['3', '4']])
        result = merge_tile_results({}, [record(0, 0, 120, 120, [0, 0, 120, 70], [top]),
                                         record(0, 50, 120, 100, [0, 70, 120, 150], [bottom])], 'local')
        joined = result['blocks'][0]
        self.assertEqual(len(result['blocks']), 1)
        self.assertEqual(joined['rows'], [['HEADER', ''], ['1', '2'], ['3', '4']])
        self.assertEqual(next(cell for cell in joined['cells'] if cell['row'] == 0)['colspan'], 2)

    def test_ambiguous_table_grid_keeps_all_observations(self):
        left = table([0, 50, 100], [0, 40, 80], [['A', 'B'], ['C', 'D']])
        right = table([0, 60, 100], [0, 40, 80], [['B', 'E'], ['D', 'F']])
        result = merge_tile_results({}, [record(0, 0, 120, 100, [0, 0, 70, 100], [left]),
                                         record(50, 0, 120, 100, [70, 0, 170, 100], [right])], 'local')
        self.assertEqual(len(result['blocks']), 2)
        self.assertIn(AMBIGUOUS, result['warnings'])

    def test_neighbour_without_detected_grid_does_not_repeat_table_text(self):
        detected = table([20, 100, 200], [10, 50, 90], [['A', '1234'], ['C', 'D']])
        detected['cells'][1]['text_blocks'] = [line('1234', 130, 20, 40)]
        records = [record(0, 0, 240, 100, [0, 0, 150, 100], [detected]),
                   record(100, 0, 180, 100, [150, 0, 280, 100], [line('1234', 30, 20, 40)])]
        result = merge_tile_results({}, records, 'local')
        self.assertEqual(len(result['blocks']), 1)
        self.assertEqual(result['blocks'][0]['rows'][0][1], '1234')

    def test_number_prefix_disagreement_is_not_assumed_truncation(self):
        result = merge_tile_results({}, [record(0, 0, 120, 80, [0, 0, 60, 80], [line('123', 40, 10, 60)]),
                                         record(30, 0, 120, 80, [60, 0, 150, 80], [line('1234', 10, 10, 60)])], 'local')
        self.assertEqual([b['text'] for b in result['blocks']], ['123', '1234'])
        self.assertIn(AMBIGUOUS, result['warnings'])

    def test_graphic_crossing_four_cores_preserves_exact_pixels_and_tiny_fragment(self):
        build = Path(__file__).resolve().parents[1] / 'build'
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as folder:
            folder = Path(folder)
            source = np.arange(100 * 100 * 3, dtype=np.uint8).reshape(100, 100, 3)
            records = []
            for index, (x, y, core) in enumerate([(0, 0, [0, 0, 50, 50]), (40, 0, [50, 0, 100, 50]),
                                                  (0, 40, [0, 50, 50, 100]), (40, 40, [50, 50, 100, 100])]):
                tile_path = folder / f'tile-{index}.png'
                Image.fromarray(source[y:min(100, y + 60), x:min(100, x + 60)]).save(tile_path)
                # Figure extends 1px into the right-hand core.
                bx, by, br, bb = max(x, 20), max(y, 20), min(x + 60, 51), min(y + 60, 85)
                asset_path = folder / f'figure-{index}.png'
                Image.fromarray(source[by:bb, bx:br]).save(asset_path)
                block = {'type': 'image', 'x': bx - x, 'y': by - y, 'width': br - bx, 'height': bb - by,
                         'image_url': f'/api/assets/{folder.name}/{asset_path.name}'}
                records.append(record(x, y, 60, 60, core, [block], str(tile_path)))
            result = merge_tile_results({}, records, 'local')
            restored = np.zeros_like(source)
            ownership = np.zeros((100, 100), np.uint8)
            for block in result['blocks']:
                x, y, w, h = [int(block[k]) for k in ('x', 'y', 'width', 'height')]
                with Image.open(folder / block['image_url'].rsplit('/', 1)[-1]) as asset:
                    restored[y:y + h, x:x + w] = np.asarray(asset)
                ownership[y:y + h, x:x + w] += 1
            self.assertTrue(np.array_equal(restored[20:85, 20:51], source[20:85, 20:51]))
            self.assertTrue(np.all(ownership[20:85, 20:51] == 1))
            self.assertEqual(int(ownership.sum()), 65 * 31)
            self.assertTrue(any(block['width'] == 1 for block in result['blocks']))

    def test_graphic_detected_only_in_neighbour_halo_is_preserved(self):
        build = Path(__file__).resolve().parents[1] / 'build'
        with tempfile.TemporaryDirectory(dir=build) as folder:
            path = Path(folder) / 'tile.png'
            Image.new('RGB', (100, 100), 'red').save(path)
            block = {'type': 'image', 'x': 60, 'y': 10, 'width': 20, 'height': 20,
                     'image_url': '/api/assets/missing.png'}
            result = merge_tile_results({}, [record(0, 0, 100, 100, [0, 0, 50, 100], [block], str(path))], 'local')
            self.assertEqual(len(result['blocks']), 1)
            self.assertEqual(result['blocks'][0]['x'], 60)


if __name__ == '__main__':
    unittest.main()
