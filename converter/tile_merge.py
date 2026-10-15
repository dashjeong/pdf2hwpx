"""Join overlapping raster observations using source geometry, never text alone.

OCR disagreement is retained and reported for source comparison. Graphics are
cropped into disjoint source rectangles; their small seam fragments are kept.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from PIL import Image


AMBIGUOUS = "분할 경계에서 글자 또는 표 구조가 서로 달라 자동 결합하지 않은 항목이 있습니다. 원본과 대조해 주세요."


def _rect(block):
    return (block['x'], block['y'], block['x'] + block['width'], block['y'] + block['height'])


def _intersection(a, b):
    value = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return value if value[2] > value[0] and value[3] > value[1] else None


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _box(block, box):
    block.update(x=box[0], y=box[1], width=box[2] - box[0], height=box[3] - box[1])


def _shift(block, tile, index):
    block = copy.deepcopy(block)
    block['x'] += tile['x']
    block['y'] += tile['y']
    if block.get('polygon'):
        block['polygon'] = [[x + tile['x'], y + tile['y']] for x, y in block['polygon']]
    for cell in block.get('cells', []):
        cell.update(_shift(cell, tile, index))
    for line in block.get('text_blocks', []):
        line.update(_shift(line, tile, index))
    block['_tiles'] = {index}
    block['_tile'] = tile
    return block


def _plain(value):
    return ''.join(value.split())


def _same_line(a, b):
    vertical_a = a['height'] > a['width'] * 2 and len(_plain(a.get('text', ''))) > 1
    vertical_b = b['height'] > b['width'] * 2 and len(_plain(b.get('text', ''))) > 1
    if vertical_a != vertical_b:
        return False, False
    cross, extent = ('x', 'width') if vertical_a else ('y', 'height')
    distance = abs(a[cross] + a[extent] / 2 - b[cross] - b[extent] / 2)
    common = min(a[cross] + a[extent], b[cross] + b[extent]) - max(a[cross], b[cross])
    coincident_top = (abs(a[cross] - b[cross]) <= max(2, min(a[extent], b[extent]) * .25) and
                      common >= min(a[extent], b[extent]) * .85)
    # A nearby repeated line must remain independently editable.
    return distance <= max(2, min(a[extent], b[extent]) * .35) or coincident_top, vertical_a


def _score(block):
    value = block.get('confidence')
    return float(value) if isinstance(value, (int, float)) else 0.0


def _union(a, b):
    ra, rb = _rect(a), _rect(b)
    return min(ra[0], rb[0]), min(ra[1], rb[1]), max(ra[2], rb[2]), max(ra[3], rb[3])


def _join_pair(a, b):
    """Return a combined observation only with baseline and spatial evidence."""
    if a['_tiles'] & b['_tiles']:
        return None
    same, vertical = _same_line(a, b)
    overlap = _intersection(_rect(a), _rect(b))
    if not same or not overlap:
        return None
    sa, sb = _plain(a.get('text', '')), _plain(b.get('text', ''))
    if not sa or not sb:
        return None
    fraction = _area(overlap) / max(1, min(_area(_rect(a)), _area(_rect(b))))
    # Whole-line alternatives: a complete containing reading takes precedence.
    if fraction >= .65 and (sa == sb or sa in sb or sb in sa):
        if sa != sb and re.findall(r'\d+', sa) != re.findall(r'\d+', sb):
            # Equal-size observations with different numbers are competing
            # readings, including 123 versus 1234. Keep both for comparison.
            smaller, larger = sorted((a, b), key=lambda c: _area(_rect(c)))
            if _area(_rect(smaller)) >= _area(_rect(larger)) * .8:
                return None
        chosen = max((a, b), key=lambda c: (len(_plain(c['text'])), _score(c)))
        combined = copy.deepcopy(chosen)
        combined['_tiles'] = a['_tiles'] | b['_tiles']
        return combined
    axis, extent = ('y', 'height') if vertical else ('x', 'width')
    left, right = sorted((a, b), key=lambda c: c[axis])
    if right[axis] <= left[axis] + 2 or right[axis] + right[extent] <= left[axis] + left[extent] + 2:
        return None
    sleft, sright = _plain(left['text']), _plain(right['text'])
    physical_overlap = min(left[axis] + left[extent], right[axis] + right[extent]) - right[axis]
    # Repeated words/numerals are not enough: their projected positions must
    # coincide within OCR's character-width uncertainty in BOTH observations.
    # Periodic numbers can have a long textual match far outside the physical
    # overlap. Try shorter exact matches rather than stopping at that decoy.
    shared = next((length for length in range(min(len(sleft), len(sright)), 0, -1)
                   if sleft[-length:] == sright[:length] and
                   all(.45 * expected <= physical_overlap <= 1.85 * expected + 3
                       for expected in (length * left[extent] / len(sleft),
                                        length * right[extent] / len(sright)))), 0)
    if not shared:
        return None
    if shared == 1 and not (sleft[-1].isascii() and sleft[-1].isdigit()):
        return None
    count, cut = 0, 0
    for index, char in enumerate(right['text']):
        if not char.isspace():
            count += 1
        if count == shared:
            cut = index + 1
            break
    remainder = right['text'][cut:]
    combined = copy.deepcopy(left)
    combined['text'] = left['text'].rstrip() + remainder
    combined['_tiles'] = a['_tiles'] | b['_tiles']
    _box(combined, _union(a, b))
    x0, y0, x1, y1 = _rect(combined)
    combined['polygon'] = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    scores = [c.get('confidence') for c in (a, b) if isinstance(c.get('confidence'), (int, float))]
    combined['confidence'] = round(min(scores), 4) if scores else None
    return combined


def _merge_lines(lines, warnings):
    def keys(line):
        x0, y0, x1, y1 = _rect(line)
        return {(x, y) for x in range(int(x0 // 128), int(x1 // 128) + 1)
                for y in range(int(y0 // 128), int(y1 // 128) + 1)}

    retained = []
    locations, index_keys = {}, {}
    for line in sorted(lines, key=lambda c: (c['y'], c['x'])):
        merged = False
        candidates = set().union(*(locations.get(key, set()) for key in keys(line)))
        for index in sorted(candidates):
            previous = retained[index]
            result = _join_pair(previous, line)
            if result is not None:
                for key in index_keys[index]:
                    locations[key].discard(index)
                retained[index] = result
                merged = True
                break
        if not merged:
            index = len(retained)
            retained.append(line)
        index_keys[index] = keys(retained[index])
        for key in index_keys[index]:
            locations.setdefault(key, set()).add(index)
    for index, a in enumerate(retained):
        candidates = set().union(*(locations.get(key, set()) for key in index_keys[index]))
        for other in sorted(candidate for candidate in candidates if candidate > index):
            b = retained[other]
            if not a['_tiles'] & b['_tiles'] and _same_line(a, b)[0] and _intersection(_rect(a), _rect(b)):
                warnings.append(AMBIGUOUS)
    return retained


def _boundaries(table, axis):
    extent = 'width' if axis == 'x' else 'height'
    dimensions = 'column_widths' if axis == 'x' else 'row_heights'
    values = [table[axis]]
    for width in table.get(dimensions, []):
        values.append(values[-1] + width)
    for cell in table.get('cells', []):
        values.extend((cell[axis], cell[axis] + cell[extent]))
    values.append(table[axis] + table[extent])
    return sorted(set(values))


def _shared(a, b):
    return sum(any(abs(value - other) <= 3 for other in b) for value in a)


def _same_table(a, b):
    if a['_tiles'] & b['_tiles']:
        return False
    ra, rb = _rect(a), _rect(b)
    near = ra[0] <= rb[2] + 3 and rb[0] <= ra[2] + 3 and ra[1] <= rb[3] + 3 and rb[1] <= ra[3] + 3
    return near and (_shared(_boundaries(a, 'x'), _boundaries(b, 'x')) >= 2 or
                     _shared(_boundaries(a, 'y'), _boundaries(b, 'y')) >= 2)


def _cluster(values):
    groups = []
    for value in sorted(values):
        if not groups or value - groups[-1][-1] > 3:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _edge(cell, axis, end):
    tile = cell['_tile']
    extent = 'width' if axis == 'x' else 'height'
    endpoint = cell[axis] + (cell[extent] if end else 0)
    limit = tile[axis] + (tile.get(extent, 0) if end else 0)
    return extent in tile and abs(endpoint - limit) <= 4


def _matching_cell(a, b):
    ra, rb = _rect(a), _rect(b)
    if all(abs(x - y) <= 3 for x, y in zip(ra, rb)):
        return True
    if not _intersection(ra, rb):
        return False
    for axis, extent in (('x', 'width'), ('y', 'height')):
        cross, cross_extent = ('y', 'height') if axis == 'x' else ('x', 'width')
        if abs(a[cross] - b[cross]) > 3 or abs(a[cross] + a[cross_extent] - b[cross] - b[cross_extent]) > 3:
            continue
        smaller, larger = sorted((a, b), key=lambda c: c[extent])
        if (larger[axis] - 3 <= smaller[axis] and
                smaller[axis] + smaller[extent] <= larger[axis] + larger[extent] + 3 and
                ((abs(smaller[axis] - larger[axis]) <= 3 and _edge(smaller, axis, True)) or
                 (abs(smaller[axis] + smaller[extent] - larger[axis] - larger[extent]) <= 3 and _edge(smaller, axis, False)))):
            return True
        left, right = sorted((a, b), key=lambda c: c[axis])
        # A seam-truncated merged cell, supported by coinciding other borders.
        if _edge(left, axis, True) and _edge(right, axis, False):
            return True
    return False


def _cell_lines(cell):
    if cell.get('text_blocks'):
        return cell['text_blocks']
    if not cell.get('text'):
        return []
    line = {key: cell[key] for key in ('x', 'y', 'width', 'height', '_tiles', '_tile')}
    line.update(type='text', text=cell['text'], confidence=cell.get('confidence'))
    return [line]


def _render_cell_lines(lines):
    grouped = []
    for line in sorted(lines, key=lambda c: (c['y'], c['x'])):
        group = next((group for group in grouped if _same_line(group[0], line)[0]), None)
        if group is None:
            grouped.append([line])
        else:
            group.append(line)
    return '\n'.join(' '.join(line['text'] for line in sorted(group, key=lambda c: (c['x'], c['y'])))
                     for group in grouped)


def _attach_table_text(texts, tables, warnings):
    """A neighbouring crop may see text while its clipped grid is undetected."""
    outside = []
    for text in texts:
        cx, cy = text['x'] + text['width'] / 2, text['y'] + text['height'] / 2
        target = next(((table, cell) for table in tables for cell in table.get('cells', [])
                       if not text['_tiles'] & cell['_tiles'] and
                       cell['x'] <= cx < cell['x'] + cell['width'] and
                       cell['y'] <= cy < cell['y'] + cell['height'] and
                       _area(_intersection(_rect(text), _rect(cell)) or (0, 0, 0, 0)) >= _area(_rect(text)) * .8), None)
        if target is None:
            outside.append(text)
        else:
            table, cell = target
            lines = _merge_lines(_cell_lines(cell) + [text], warnings)
            cell.update(text_blocks=lines, text=_render_cell_lines(lines))
            table['rows'][cell['row']][cell['col']] = cell['text']
    return outside


def _merge_tables(tables, warnings):
    groups = []
    for table in tables:
        matching = [group for group in groups if any(_same_table(table, other) for other in group)]
        if not matching:
            groups.append([table])
        else:
            merged = [table]
            for group in matching:
                merged.extend(group)
                groups.remove(group)
            groups.append(merged)
    result = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
            continue
        cells = []
        for table in group:
            if not table.get('cells'):
                warnings.append(AMBIGUOUS)
                result.extend(group)
                break
            for cell in table['cells']:
                found = next((c for c in cells if not c['_tiles'] & cell['_tiles'] and _matching_cell(c, cell)), None)
                if found is None:
                    cells.append(copy.deepcopy(cell))
                    continue
                lines = _merge_lines(_cell_lines(found) + _cell_lines(cell), warnings)
                found.update(text=_render_cell_lines(lines),
                             text_blocks=lines, _tiles=found['_tiles'] | cell['_tiles'])
                _box(found, _union(found, cell))
        else:
            xs = _cluster(value for cell in cells for value in (cell['x'], cell['x'] + cell['width']))
            ys = _cluster(value for cell in cells for value in (cell['y'], cell['y'] + cell['height']))
            # Distinct overlapping cells indicate inconsistent seam grids.
            if any(_intersection(_rect(a), _rect(b)) and _area(_intersection(_rect(a), _rect(b))) > 9
                   for i, a in enumerate(cells) for b in cells[i + 1:]):
                warnings.append(AMBIGUOUS)
                result.extend(group)
                continue
            table = copy.deepcopy(group[0])
            table['rows'] = [[''] * (len(xs) - 1) for _ in range(len(ys) - 1)]
            for cell in cells:
                c0, c1 = [min(range(len(xs)), key=lambda i: abs(xs[i] - value)) for value in (cell['x'], cell['x'] + cell['width'])]
                r0, r1 = [min(range(len(ys)), key=lambda i: abs(ys[i] - value)) for value in (cell['y'], cell['y'] + cell['height'])]
                cell.update(row=r0, col=c0, rowspan=r1 - r0, colspan=c1 - c0)
                table['rows'][r0][c0] = cell.get('text', '')
            table.update(cells=cells, column_widths=[b - a for a, b in zip(xs, xs[1:])],
                         row_heights=[b - a for a, b in zip(ys, ys[1:])],
                         _tiles=set().union(*(item['_tiles'] for item in group)))
            _box(table, (xs[0], ys[0], xs[-1], ys[-1]))
            table.pop('polygon', None)
            result.append(table)
    return result


def _subtract(box, covered):
    overlap = _intersection(box, covered)
    if not overlap:
        return [box]
    x0, y0, x1, y1 = box
    a, b, c, d = overlap
    pieces = [(x0, y0, x1, b), (x0, d, x1, y1), (x0, b, a, d), (c, b, x1, d)]
    return [piece for piece in pieces if _area(piece) > 0]


def _crop_graphic(block, box, number, warnings):
    tile = block['_tile']
    folder = Path(tile['path']).parent
    filename = str(block.get('image_url', '')).rsplit('/', 1)[-1]
    source = folder / filename
    # Figure assets may have text removed, so prefer them to the original tile.
    asset = bool(filename and source.is_file())
    if not asset:
        source = Path(tile['path'])
    if not source.is_file():
        warnings.append(AMBIGUOUS)
        return None
    reference = _rect(block) if asset else (tile['x'], tile['y'], tile['x'] + tile['width'], tile['y'] + tile['height'])
    with Image.open(source) as image:
        sx, sy = image.width / (reference[2] - reference[0]), image.height / (reference[3] - reference[1])
        crop = (round((box[0] - reference[0]) * sx), round((box[1] - reference[1]) * sy),
                round((box[2] - reference[0]) * sx), round((box[3] - reference[1]) * sy))
        target = folder / f'{Path(tile["path"]).stem}-seam-figure-{number}.png'
        image.crop(crop).save(target)
    copied = copy.deepcopy(block)
    _box(copied, box)
    copied.pop('polygon', None)
    copied['image_url'] = f'/api/assets/{folder.name}/{target.name}'
    return copied


def _merge_graphics(graphics, warnings):
    owned, halos = [], []
    for block in graphics:
        own = _intersection(_rect(block), block['_tile']['core'])
        if own:
            owned.append((block, own))
        halos.append((block, _rect(block)))
    covered, result = [], []
    # Core-owned observations first, then any source ink observed only in a
    # neighbour's context. Rectangle subtraction gives exact pixel ownership.
    for block, box in owned + halos:
        pending = [box]
        for existing in covered:
            pending = [part for candidate in pending for part in _subtract(candidate, existing)]
        for part in pending:
            cropped = _crop_graphic(block, part, len(result), warnings)
            if cropped:
                result.append(cropped)
                covered.append(part)
    return result


def _recheck_seam_cells(tables, records, engine, warnings):
    """Read a bounded complete source cell when crop observations disagree.

    No provider credentials are available here; only offline OCR is eligible.
    The third observation must corroborate an existing reading, or improve a
    low-confidence reading substantially. Alternatives remain reviewable.
    """
    if engine != 'local' or not records:
        return
    from .ocr import recognize_page
    serial = 0
    max_pixels = max(record['tile'].get('width', 0) * record['tile'].get('height', 0) for record in records)
    for table in tables:
        for cell in table.get('cells', []):
            observed = _cell_lines(cell)
            values = {_plain(line['text']) for line in observed if line.get('text')}
            scores = [_score(line) for line in observed]
            conflict = any(_plain(a.get('text', '')) != _plain(b.get('text', '')) and
                           _same_line(a, b)[0] and _intersection(_rect(a), _rect(b))
                           for i, a in enumerate(observed) for b in observed[i + 1:])
            if not conflict and (not scores or min(scores) >= .85):
                continue
            x0, y0, x1, y1 = _rect(cell)
            # Exclude actual ruled borders, as normal cell OCR does.
            crop_box = (int(x0) + 3, int(y0) + 3, int(x1) - 2, int(y1) - 2)
            width, height = crop_box[2] - crop_box[0], crop_box[3] - crop_box[1]
            if min(width, height) < 1 or width * height > max_pixels:
                continue
            relevant = [(record['tile'], _intersection(crop_box, (record['tile']['x'], record['tile']['y'],
                         record['tile']['x'] + record['tile'].get('width', 0),
                         record['tile']['y'] + record['tile'].get('height', 0)))) for record in records]
            relevant = [(tile, overlap) for tile, overlap in relevant if overlap]
            if not relevant or any(not Path(tile['path']).is_file() for tile, _ in relevant):
                continue
            canvas = Image.new('RGB', (width, height), 'white')
            uncovered = [crop_box]
            for tile, overlap in relevant:
                with Image.open(tile['path']) as image:
                    tile_crop = tuple(round(value) for value in (overlap[0] - tile['x'], overlap[1] - tile['y'],
                                                                 overlap[2] - tile['x'], overlap[3] - tile['y']))
                    canvas.paste(image.crop(tile_crop), (round(overlap[0] - crop_box[0]), round(overlap[1] - crop_box[1])))
                uncovered = [part for candidate in uncovered for part in _subtract(candidate, overlap)]
            if uncovered:
                canvas.close()
                continue
            source = Path(relevant[0][0]['path'])
            crop_path = source.parent / f'{source.stem}-seam-cell-{serial}.png'
            serial += 1
            canvas.save(crop_path)
            canvas.close()
            try:
                reread = recognize_page(crop_path, 'local', {'detect_tables': False, 'preserve_images': False,
                                         'upscale_small_images': False, 'deskew': False})
                lines = [line for line in reread.get('blocks', []) if line.get('type') == 'text' and line.get('text')]
                text = _render_cell_lines(lines)
                confidence = min((_score(line) for line in lines), default=0)
                if not text or confidence < .85 or (values and _plain(text) not in values and
                        not (max(scores, default=0) < .85 and confidence >= max(scores, default=0) + .1)):
                    continue
                if confidence < max(scores, default=0) - .1:
                    continue
                alternatives = [{key: line[key] for key in ('text', 'x', 'y', 'width', 'height', 'confidence', 'polygon')
                                 if key in line} for line in observed if _plain(line.get('text', '')) != _plain(text)]
                for line in lines:
                    line['x'] += crop_box[0]
                    line['y'] += crop_box[1]
                    if line.get('polygon'):
                        line['polygon'] = [[x + crop_box[0], y + crop_box[1]] for x, y in line['polygon']]
                    line['_tiles'], line['_tile'] = set(cell['_tiles']), cell['_tile']
                cell.update(text=text, confidence=confidence, text_blocks=lines, seam_reocr=True)
                if alternatives:
                    cell['seam_alternatives'] = alternatives
                    warnings.append('분할 경계의 일부 셀은 원본 조각을 합쳐 다시 읽었습니다. 서로 달랐던 숫자 후보를 원본과 대조해 주세요.')
                table['rows'][cell['row']][cell['col']] = text
            except Exception:
                warnings.append(AMBIGUOUS)
            finally:
                crop_path.unlink(missing_ok=True)


def _clean(block):
    for key in list(block):
        if key.startswith('_'):
            block.pop(key)
    for child in block.get('cells', []) + block.get('text_blocks', []):
        _clean(child)


def _merge_form_shapes(shapes):
    """Join coincident rules observed in overlapping raster tiles only."""
    retained = []
    for block in shapes:
        if block.get('type') != 'shape' or block.get('shape') != 'line':
            retained.append(block)
            continue
        horizontal = block['height'] <= .01 and block['width'] > 0
        vertical = block['width'] <= .01 and block['height'] > 0
        if not horizontal and not vertical:
            retained.append(block)
            continue
        axis, extent, cross = ('x', 'width', 'y') if horizontal else ('y', 'height', 'x')
        matched = False
        for other in retained:
            if other.get('type') != 'shape' or other.get('shape') != 'line' or other['_tiles'] & block['_tiles']:
                continue
            same_orientation = other['height'] <= .01 if horizontal else other['width'] <= .01
            if not same_orientation or other.get('stroke_color') != block.get('stroke_color'):
                continue
            if abs(other.get('stroke_width', 1) - block.get('stroke_width', 1)) > 1:
                continue
            if abs(other[cross] - block[cross]) > 1:
                continue
            overlap = min(other[axis] + other[extent], block[axis] + block[extent]) - max(other[axis], block[axis])
            if overlap <= 0:
                continue
            start, stop = min(other[axis], block[axis]), max(other[axis] + other[extent], block[axis] + block[extent])
            other[axis], other[extent] = start, stop - start
            other['points'] = [[0, 0], [other['width'], other['height']]]
            other['_tiles'].update(block['_tiles'])
            matched = True
            break
        if not matched:
            retained.append(block)
    return retained


def merge_tile_results(page, records, engine, warnings=None):
    warnings = list(warnings or [])
    texts, tables, graphics, others = [], [], [], []
    for index, record in enumerate(records):
        tile, observed = record['tile'], record['result']
        warnings.extend(observed.get('warnings', []))
        # Old manifests/probes omit tile dimensions; OCR page dimensions or
        # observed geometry still provide a bounded source context.
        tile = dict(tile)
        tile.setdefault('width', observed.get('width', max([tile['core'][2] - tile['x']] + [b['x'] + b['width'] for b in observed.get('blocks', [])])))
        tile.setdefault('height', observed.get('height', max([tile['core'][3] - tile['y']] + [b['y'] + b['height'] for b in observed.get('blocks', [])])))
        for original in observed.get('blocks', []):
            block = _shift(original, tile, index)
            {'text': texts, 'table': tables, 'image': graphics}.get(block.get('type'), others).append(block)
    tables = _merge_tables(tables, warnings)
    texts = _attach_table_text(_merge_lines(texts, warnings), tables, warnings)
    _recheck_seam_cells(tables, records, engine, warnings)
    blocks = texts + tables + _merge_graphics(graphics, warnings) + _merge_form_shapes(others)
    for index, block in enumerate(blocks):
        block['id'] = f'tile-merged-{index}'
        _clean(block)
    from .global_tables import recover_global_tables
    blocks = recover_global_tables(page, records, blocks, engine, warnings)
    for index, block in enumerate(blocks):
        block['id'] = f'tile-merged-{index}'
    page['tile_count'] = len(records)
    warnings.append('큰 이미지를 메모리 예산에 맞춰 분할 인식하고 원본 좌표의 한 페이지로 결합했습니다. 분할 경계의 글자와 표를 원본과 확인해 주세요.')
    return {'engine': engine, 'blocks': sorted(blocks, key=lambda b: (b['y'], b['x'])), 'warnings': list(dict.fromkeys(warnings))}
