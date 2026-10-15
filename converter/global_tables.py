"""Recover whole-page ruled grids from a bounded preview and original tile pixels."""
from __future__ import annotations

import copy
import math
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
from PIL import Image

MAX_PREVIEW_PIXELS = 4_000_000
REVIEW = "분할 경계의 표는 원본 조각을 합쳐 셀별로 다시 읽었습니다. 서로 다른 숫자 후보와 셀 병합을 원본과 대조해 주세요."


def _rect(block):
    return block['x'], block['y'], block['x'] + block['width'], block['y'] + block['height']


def _intersection(a, b):
    value = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return value if value[2] > value[0] and value[3] > value[1] else None


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _subtract(box, covering):
    overlap = _intersection(box, covering)
    if not overlap:
        return [box]
    x0, y0, x1, y1 = box
    a, b, c, d = overlap
    return [p for p in [(x0, y0, x1, b), (x0, d, x1, y1), (x0, b, a, d), (c, b, x1, d)] if _area(p)]


def compose_source_crop(records, box):
    """A crop no larger than one tile; tile PNGs are decoded one at a time."""
    box = tuple(int(value) for value in box)
    width, height = box[2] - box[0], box[3] - box[1]
    ceiling = max((r['tile'].get('width', 0) * r['tile'].get('height', 0) for r in records), default=0)
    if min(width, height) < 1 or width * height > ceiling:
        return None
    remaining, canvas = [box], Image.new('RGB', (width, height), 'white')
    for record in records:
        tile = record['tile']
        bounds = tile['x'], tile['y'], tile['x'] + tile.get('width', 0), tile['y'] + tile.get('height', 0)
        parts = [(piece, _intersection(piece, bounds)) for piece in remaining]
        parts = [(piece, overlap) for piece, overlap in parts if overlap]
        if not parts:
            continue
        source = Path(tile['path'])
        if not source.is_file():
            canvas.close()
            return None
        with Image.open(source) as image:
            for _, overlap in parts:
                local = tuple(int(v) for v in (overlap[0] - tile['x'], overlap[1] - tile['y'],
                                               overlap[2] - tile['x'], overlap[3] - tile['y']))
                with image.crop(local) as cropped:
                    canvas.paste(cropped, (int(overlap[0] - box[0]), int(overlap[1] - box[1])))
                remaining = [p for piece in remaining for p in _subtract(piece, overlap)]
        if not remaining:
            return canvas
    canvas.close()
    return None


def _preview(page, records):
    if not records or not page.get('image_url') or not page.get('width') or not page.get('height'):
        return None
    folder = Path(records[0]['tile']['path']).resolve().parent
    if any(Path(record['tile']['path']).resolve().parent != folder for record in records):
        return None
    reference = urlsplit(str(page['image_url']))
    parts = reference.path.split('/')
    if reference.scheme or reference.netloc or len(parts) != 5 or parts[1:3] != ['api', 'assets'] or parts[3] != folder.name:
        return None
    path = (folder / parts[-1]).resolve()
    if path.parent != folder or path.suffix.lower() != '.png' or not path.is_file():
        return None
    with Image.open(path) as image:
        if image.width * image.height > MAX_PREVIEW_PIXELS:
            return None
        rgb = np.asarray(image.convert('RGB'))
    return folder, rgb, page['width'] / rgb.shape[1], page['height'] / rgb.shape[0]


def _observations(blocks):
    result = []
    for block in blocks:
        if block.get('type') == 'text':
            result.append(block)
        for cell in block.get('cells', []):
            if cell.get('text_blocks'):
                result.extend(cell['text_blocks'])
            elif cell.get('text'):
                result.append({**cell, 'type': 'text'})
    return result


def _inside(block, box, tolerance=5):
    x, y = block['x'] + block['width'] / 2, block['y'] + block['height'] / 2
    return box[0] - tolerance <= x <= box[2] + tolerance and box[1] - tolerance <= y <= box[3] + tolerance


def _preview_region(rgb, box, sx, sy):
    x0, y0, x1, y1 = box
    return rgb[max(0, int(y0 / sy)):min(rgb.shape[0], math.ceil(y1 / sy)),
               max(0, int(x0 / sx)):min(rgb.shape[1], math.ceil(x1 / sx))]


def _colored_empty_grid(block, rgb, sx, sy):
    if block.get('type') != 'table' or any(str(value).strip() for row in block.get('rows', []) for value in row):
        return False
    region = _preview_region(rgb, _rect(block), sx, sy)
    if not region.size:
        return False
    hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
    saturated = np.mean((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 60))
    neutral_dark = np.mean((hsv[:, :, 1] < 50) & (hsv[:, :, 2] < 100))
    # Strongly colored adjacent fills without dark rules are a bitmap grid.
    # Gray forms and colored forms with real dark borders remain editable.
    return saturated > .6 and neutral_dark < .005


def _graphic_bounds(block, rgb, sx, sy):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    mask = ((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 60)).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    box = _rect(block)
    components = []
    for x, y, width, height, area in stats[1:]:
        candidate = x * sx, y * sy, (x + width) * sx, (y + height) * sy
        if area >= 100 and _intersection(candidate, box):
            components.append(candidate)
    if not components:
        return box
    padding = max(sx, sy) * 3
    return (max(0, math.floor(min(b[0] for b in components) - padding)),
            max(0, math.floor(min(b[1] for b in components) - padding)),
            min(round(rgb.shape[1] * sx), math.ceil(max(b[2] for b in components) + padding)),
            min(round(rgb.shape[0] * sy), math.ceil(max(b[3] for b in components) + padding)))


def _text(lines):
    from .tile_merge import _render_cell_lines
    return _render_cell_lines(lines)


def _score(block):
    value = block.get('confidence')
    return float(value) if isinstance(value, (int, float)) else 0.0


def recover_global_tables(page, records, blocks, engine, warnings):
    """Replace fragment grids only with verified whole-source cell evidence."""
    preview = _preview(page, records)
    if preview is None:
        return blocks
    folder, rgb, sx, sy = preview
    from .ocr import _find_tables, recognize_page
    observed = _observations(blocks)
    recovered, replacements, graphic_regions = [], [], []
    # Preserve saturated graphics that a crop detector mistook for empty cells.
    for index, block in enumerate(blocks):
        if not _colored_empty_grid(block, rgb, sx, sy):
            continue
        bounds = _graphic_bounds(block, rgb, sx, sy)
        if any(_intersection(bounds, existing) for existing in graphic_regions):
            continue
        crop = compose_source_crop(records, bounds)
        if crop is None:
            continue
        filename = f'global-source-graphic-{index}.png'
        crop.save(folder / filename)
        crop.close()
        recovered.append({'type': 'image', 'x': bounds[0], 'y': bounds[1],
                          'width': bounds[2] - bounds[0], 'height': bounds[3] - bounds[1],
                          'image_url': f'/api/assets/{folder.name}/{filename}', 'source_graphic_preserved': True})
        graphic_regions.append(bounds)
        warnings.append('색상 칸으로 된 그림은 빈 표로 바꾸지 않고 원본 화소를 그대로 보존했습니다.')
    grid_preview = rgb.copy()
    for box in graphic_regions:
        x0, y0, x1, y1 = box
        grid_preview[max(0, int(y0 / sy)):min(rgb.shape[0], math.ceil(y1 / sy)),
                     max(0, int(x0 / sx)):min(rgb.shape[1], math.ceil(x1 / sx))] = 255
    whole_tables, _ = _find_tables(cv2.cvtColor(grid_preview, cv2.COLOR_RGB2BGR))
    from .editable_graphics import sample_table_styles
    sample_table_styles(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), whole_tables)
    for number, table in enumerate(whole_tables):
        mapped = copy.deepcopy(table)
        for target in [mapped] + mapped['cells']:
            for key, scale in (('x', sx), ('width', sx), ('y', sy), ('height', sy)):
                target[key] = round(target[key] * scale, 2)
            if 'border_width' in target:
                target['border_width'] *= (sx + sy) / 2
        mapped['column_widths'] = [round(value * sx, 2) for value in mapped['column_widths']]
        mapped['row_heights'] = [round(value * sy, 2) for value in mapped['row_heights']]
        if _colored_empty_grid(mapped, rgb, sx, sy):
            continue
        clear, changed, scores = True, False, []
        for position, cell in enumerate(mapped['cells']):
            cell.pop('_texts', None)
            cell.pop('_scores', None)
            box = _rect(cell)
            original = [line for line in observed if line.get('text') and _inside(line, box, 0)]
            cell['text_blocks'] = copy.deepcopy(original)
            cell['text'] = _text(original)
            cell['confidence'] = min((_score(line) for line in original), default=None)
            inset = max(4, math.ceil(max(sx, sy) * 2))
            bounds = (math.ceil(box[0]) + inset, math.ceil(box[1]) + inset,
                      math.floor(box[2]) - inset, math.floor(box[3]) - inset)
            crop = compose_source_crop(records, bounds)
            if crop is None:
                clear = False
                warnings.append(REVIEW)
                continue
            pixels = np.asarray(crop)
            gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
            blank = bool(np.std(gray) < 4 or np.count_nonzero(gray < 180) < 6)
            if engine == 'local' and not blank:
                filename = folder / f'.global-table-{number}-cell-{position}.png'
                crop.save(filename)
                try:
                    result = recognize_page(filename, 'local', {'detect_tables': False, 'cell_ocr': False,
                               'preserve_images': False, 'upscale_small_images': False, 'deskew': False})
                    lines = [copy.deepcopy(line) for line in result.get('blocks', []) if line.get('type') == 'text' and line.get('text')]
                    confidence = min((_score(line) for line in lines), default=0)
                    if lines and confidence >= .85:
                        for line in lines:
                            line['x'] += bounds[0]
                            line['y'] += bounds[1]
                            if line.get('polygon'):
                                line['polygon'] = [[x + bounds[0], y + bounds[1]] for x, y in line['polygon']]
                        text = _text(lines)
                        alternatives = [{key: line[key] for key in ('text', 'x', 'y', 'width', 'height', 'confidence', 'polygon')
                                         if key in line} for line in original if ''.join(line['text'].split()) != ''.join(text.split())]
                        cell.update(text=text, text_blocks=lines, confidence=confidence, global_cell_reocr=True)
                        if alternatives:
                            cell['seam_alternatives'] = alternatives
                            changed = True
                    else:
                        clear = False
                except Exception:
                    clear = False
                finally:
                    filename.unlink(missing_ok=True)
            elif not blank and engine != 'local' and any(
                    ''.join(a.get('text', '').split()) != ''.join(b.get('text', '').split()) and
                    _area(_intersection(_rect(a), _rect(b)) or (0, 0, 0, 0)) >= .65 * min(_area(_rect(a)), _area(_rect(b)))
                    for i, a in enumerate(original) for b in original[i + 1:]):
                clear = False
                warnings.append(REVIEW)
            elif not blank and (not original or min((_score(line) for line in original), default=0) < .85):
                clear = False
            crop.close()
            if not blank and not cell['text'].strip():
                clear = False
            if cell['confidence'] is not None:
                scores.append(cell['confidence'])
            mapped['rows'][cell['row']][cell['col']] = cell['text']
        if clear:
            fonts = [line.get('font_size', line.get('height', 20) * .78)
                     for cell in mapped['cells'] for line in cell.get('text_blocks', [])]
            if fonts:
                mapped['font_size'] = float(np.median(fonts))
            mapped.update(type='table', confidence=sum(scores) / len(scores) if scores else None,
                          global_grid_recovered=True)
            recovered.append(mapped)
            replacements.append(_rect(mapped))
            if changed:
                warnings.append(REVIEW)
        else:
            warnings.append('미리보기에서 전체 표 선을 찾았지만 일부 셀은 확정하지 못해 기존 인식 결과를 유지했습니다. 원본과 대조해 주세요.')
    retained = []
    for block in blocks:
        is_color = any(_inside(block, box) for box in graphic_regions)
        is_table = any(_inside(block, box) for box in replacements)
        if is_color and block.get('type') in {'table', 'image'}:
            continue
        if is_table and block.get('type') in {'table', 'text'}:
            # A detected outer grid must contain the fragment, not merely its
            # centre, before removing its observations.
            extent = _rect(block)
            if any(_area(_intersection(extent, (box[0] - 5, box[1] - 5, box[2] + 5, box[3] + 5)) or (0, 0, 0, 0)) >= .95 * _area(extent)
                   for box in replacements):
                continue
        retained.append(block)
    return retained + recovered

