"""Separate editable form geometry from localized raster pictures.

Pixels used to recognize text are never modified here. This module only builds
graphic objects after OCR/native text extraction and removes their duplicates
from the residual picture layer; it never emits a page-sized background image.
"""
from __future__ import annotations

import ctypes
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def _rgb(color):
    try:
        return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    except (ValueError, TypeError):
        return (0, 0, 0)


def _dominant_color(pixels, default='#ffffff'):
    pixels = np.asarray(pixels, dtype=np.uint8)
    if pixels.ndim >= 3:
        stride = max(1, int(math.ceil(math.sqrt(pixels.shape[0] * pixels.shape[1] / 65536))))
        pixels = pixels[::stride, ::stride].reshape(-1, 3)
    else:
        pixels = pixels.reshape(-1, 3)
    pixels = pixels[::max(1, int(math.ceil(len(pixels) / 65536)))]
    if not len(pixels):
        return default
    bins = (pixels // 16).astype(np.int32)
    keys = bins[:, 0] * 256 + bins[:, 1] * 16 + bins[:, 2]
    key = np.bincount(keys, minlength=4096).argmax()
    b, g, r = np.median(pixels[keys == key], axis=0).astype(int)
    return f'#{r:02x}{g:02x}{b:02x}'


def sample_table_styles(image, tables):
    """Record visible cell fill and border colors before removing table ink."""
    height, width = image.shape[:2]
    for table in tables:
        for cell in table.get('cells', []):
            left, top = max(0, int(cell['x'])), max(0, int(cell['y']))
            right = min(width, int(math.ceil(cell['x'] + cell['width'])))
            bottom = min(height, int(math.ceil(cell['y'] + cell['height'])))
            if right <= left or bottom <= top:
                continue
            inset = min(5, max(1, (right - left) // 8), max(1, (bottom - top) // 8))
            interior = image[top + inset:bottom - inset, left + inset:right - inset]
            cell['fill_color'] = _dominant_color(interior)
            strips = [image[max(0, top - 1):min(height, top + 2), left:right].reshape(-1, 3),
                      image[max(0, bottom - 1):min(height, bottom + 2), left:right].reshape(-1, 3),
                      image[top:bottom, max(0, left - 1):min(width, left + 2)].reshape(-1, 3),
                      image[top:bottom, max(0, right - 1):min(width, right + 2)].reshape(-1, 3)]
            pixels = np.concatenate(strips)
            fill_bgr = np.asarray(_rgb(cell['fill_color'])[::-1])
            contrast = pixels[np.max(np.abs(pixels.astype(np.int16) - fill_bgr), axis=1) > 35]
            dark = contrast[np.mean(contrast, axis=1) < 230] if len(contrast) else pixels[np.mean(pixels, axis=1) < 120]
            if len(dark):
                luminance = np.mean(dark, axis=1)
                dark = dark[luminance <= np.percentile(luminance, 25)]
            cell['border_color'] = _dominant_color(dark, '#000000')
            border_bgr = np.asarray(_rgb(cell['border_color'])[::-1])
            lengths = []
            cx, cy = (left + right) // 2, (top + bottom) // 2
            for edge, coordinate, horizontal in ((left, cy, True), (right, cy, True),
                                                 (top, cx, False), (bottom, cx, False)):
                start = max(0, edge - 6)
                stop = min(width if horizontal else height, edge + 7)
                sample = image[coordinate, start:stop] if horizontal else image[start:stop, coordinate]
                matches = np.max(np.abs(sample.astype(np.int16) - border_bgr), axis=1) < 45
                nearby = np.flatnonzero(matches)
                if len(nearby):
                    center = min(nearby, key=lambda value: abs(value - (edge - start)))
                    a = b = int(center)
                    while a > 0 and matches[a - 1]:
                        a -= 1
                    while b + 1 < len(matches) and matches[b + 1]:
                        b += 1
                    if b - a + 1 <= 9:
                        lengths.append(b - a + 1)
            cell['border_width'] = float(np.median(lengths)) if lengths else 1.0
        fills = [c['fill_color'] for c in table.get('cells', []) if 'fill_color' in c]
        borders = [c['border_color'] for c in table.get('cells', []) if 'border_color' in c]
        if fills:
            table['fill_color'] = max(dict.fromkeys(fills), key=fills.count)
        if borders:
            table['border_color'] = max(dict.fromkeys(borders), key=borders.count)
            widths = [c['border_width'] for c in table.get('cells', []) if 'border_width' in c]
            table['border_width'] = float(np.median(widths)) if widths else 1.0


def table_boundary_mask(shape, tables, thickness=3):
    mask = np.zeros(shape[:2], np.uint8)
    for table in tables:
        for cell in table.get('cells', []):
            x, y = int(round(cell['x'])), int(round(cell['y']))
            r, b = int(round(cell['x'] + cell['width'])), int(round(cell['y'] + cell['height']))
            cv2.rectangle(mask, (x, y), (r, b), 255, thickness)
    return mask


def _shape(points, *, closed=False, stroke='#000000', stroke_width=1.0, fill=None, source='raster-form'):
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    if len(points) < 2 or not np.isfinite(points).all():
        return None
    left, top = points.min(axis=0)
    right, bottom = points.max(axis=0)
    kind = 'line' if len(points) == 2 and not closed else 'polyline'
    unique = np.unique(np.round(points, 3), axis=0)
    if closed and len(unique) == 4:
        xs, ys = np.unique(unique[:, 0]), np.unique(unique[:, 1])
        if len(xs) == len(ys) == 2:
            kind = 'rect'
    return {'type': 'shape', 'shape': kind, 'x': float(left), 'y': float(top),
            'width': float(right - left), 'height': float(bottom - top),
            'points': (points - [left, top]).tolist(), 'closed': bool(closed),
            'stroke_color': stroke, 'stroke_width': max(.1, float(stroke_width)) if stroke else 0.,
            'fill_color': fill if closed else None, 'source': source}


def _points(block):
    if block.get('points'):
        return np.asarray(block['points'], dtype=float) + [block['x'], block['y']]
    x, y, w, h = (block[key] for key in ('x', 'y', 'width', 'height'))
    if block.get('shape') == 'line':
        return np.asarray([[x, y], [x + w, y + h]])
    return np.asarray([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])


def _paint_mask(mask, block, extra=2, fill=True, reference=None, preserve=None):
    points = np.rint(_points(block)).astype(np.int32)
    if fill and block.get('fill_color') and block.get('closed', block.get('shape') == 'rect'):
        if reference is None:
            cv2.fillPoly(mask, [points], 255)
        else:
            left, top = np.maximum(points.min(axis=0), [0, 0])
            right, bottom = np.minimum(points.max(axis=0) + 1, [mask.shape[1], mask.shape[0]])
            if right > left and bottom > top:
                polygon = np.zeros((bottom - top, right - left), np.uint8)
                cv2.fillPoly(polygon, [(points - [left, top]).astype(np.int32)], 255)
                roi = reference[top:bottom, left:right]
                near = np.ones(polygon.shape, bool)
                for channel, color in enumerate(_rgb(block['fill_color'])[::-1]):
                    near &= np.abs(roi[:, :, channel].astype(np.int16) - color) <= 24
                mask[top:bottom, left:right][(polygon > 0) & near] = 255
                if preserve is not None:
                    preserve[top:bottom, left:right][(polygon > 0) & ~near] = 255
    if block.get('stroke_color'):
        cv2.polylines(mask, [points], bool(block.get('closed', block.get('shape') == 'rect')), 255,
                      max(1, int(math.ceil(block.get('stroke_width', 1))) + extra))


def _covered_by_table(block, table_mask):
    pad = max(2, int(math.ceil(block.get('stroke_width', 1))) + 1)
    x, y, w, h = (block[k] for k in ('x', 'y', 'width', 'height'))
    left, top = max(0, int(math.floor(x)) - pad), max(0, int(math.floor(y)) - pad)
    right, bottom = min(table_mask.shape[1], int(math.ceil(x + w)) + pad + 1), min(table_mask.shape[0], int(math.ceil(y + h)) + pad + 1)
    if right <= left or bottom <= top:
        return False
    region = table_mask[top:bottom, left:right]
    mask = np.zeros_like(region)
    local = dict(block, x=x - left, y=y - top)
    _paint_mask(mask, local, extra=0, fill=False)
    total = np.count_nonzero(mask)
    if total and np.count_nonzero(mask & region) >= total * .82:
        return True
    if block.get('fill_color'):
        x, y, w, h = (block[k] for k in ('x', 'y', 'width', 'height'))
        # Filling a genuine cell is represented by the cell's sampled style.
        top, left = max(0, int(y)), max(0, int(x))
        bottom, right = min(table_mask.shape[0], int(y + h)), min(table_mask.shape[1], int(x + w))
        if bottom > top and right > left:
            border = table_mask[top:bottom, left:right]
            return np.count_nonzero(border) / border.size > .15
    return False


def _matrix(raw, handle):
    value = raw.FS_MATRIX()
    if not raw.FPDFPageObj_GetMatrix(handle, ctypes.byref(value)):
        return np.eye(3)
    return np.asarray([[value.a, value.c, value.e], [value.b, value.d, value.f], [0., 0., 1.]])


def native_pdf_shapes(page, width, height, tables):
    """Convert PDFium linear path primitives, including nested form CTMs."""
    import pypdfium2 as pdfium
    raw = pdfium.raw
    pw, ph = page.get_size()
    sx, sy = width / pw, height / ph
    table_mask = table_boundary_mask((height, width), tables, 5)
    shapes, protected = [], []

    def walk(parent, matrix, level=0, form=False):
        counter = raw.FPDFFormObj_CountObjects if form else raw.FPDFPage_CountObjects
        getter = raw.FPDFFormObj_GetObject if form else raw.FPDFPage_GetObject
        for index in range(counter(parent)):
            obj = getter(parent, index)
            kind = raw.FPDFPageObj_GetType(obj)
            combined = matrix @ _matrix(raw, obj)
            if kind == raw.FPDF_PAGEOBJ_FORM and level < 12:
                walk(obj, combined, level + 1, True)
                continue
            if kind == raw.FPDF_PAGEOBJ_IMAGE:
                corners = (combined @ np.asarray([[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]]).T).T
                coords = np.column_stack((corners[:, 0] * sx, (ph - corners[:, 1]) * sy))
                lo, hi = coords.min(axis=0), coords.max(axis=0)
                # An image spanning the page is commonly a searchable scan,
                # whose OCR text layer does not make the form a photograph.
                if hi[0] - lo[0] < width * .85 or hi[1] - lo[1] < height * .85:
                    protected.append({'x': float(lo[0]), 'y': float(lo[1]), 'width': float(hi[0] - lo[0]), 'height': float(hi[1] - lo[1])})
                continue
            if kind != raw.FPDF_PAGEOBJ_PATH:
                continue
            fill_mode, stroked = ctypes.c_int(), ctypes.c_int()
            if not raw.FPDFPath_GetDrawMode(obj, ctypes.byref(fill_mode), ctypes.byref(stroked)):
                continue
            colors = []
            for getter_color in (raw.FPDFPageObj_GetStrokeColor, raw.FPDFPageObj_GetFillColor):
                values = [ctypes.c_uint() for _ in range(4)]
                ok = getter_color(obj, *(ctypes.byref(value) for value in values))
                colors.append((f'#{values[0].value:02x}{values[1].value:02x}{values[2].value:02x}', values[3].value) if ok else ('#000000', 255))
            stroke = colors[0][0] if stroked.value and colors[0][1] else None
            fill = colors[1][0] if fill_mode.value and colors[1][1] else None
            if not stroke and not fill:
                continue
            thickness = ctypes.c_float()
            raw.FPDFPageObj_GetStrokeWidth(obj, ctypes.byref(thickness))
            physical_width = max(.1, thickness.value * math.sqrt(abs(np.linalg.det(combined[:2, :2]))) * (sx + sy) / 2)
            paths, points, closed = [], [], False
            bezier = []
            for segment_index in range(raw.FPDFPath_CountSegments(obj)):
                segment = raw.FPDFPath_GetPathSegment(obj, segment_index)
                segment_type = raw.FPDFPathSegment_GetType(segment)
                px, py = ctypes.c_float(), ctypes.c_float()
                if not raw.FPDFPathSegment_GetPoint(segment, ctypes.byref(px), ctypes.byref(py)):
                    continue
                point = combined @ [px.value, py.value, 1.]
                mapped = [point[0] * sx, (ph - point[1]) * sy]
                if segment_type == raw.FPDF_SEGMENT_BEZIERTO:
                    bezier.append(mapped)
                    if len(bezier) == 3 and points:
                        start = np.asarray(points[-1])
                        control1, control2, end = np.asarray(bezier)
                        distance = np.linalg.norm(start - control1) + np.linalg.norm(control1 - control2) + np.linalg.norm(control2 - end)
                        steps = max(8, min(96, int(math.ceil(distance / 3))))
                        for fraction in np.linspace(0, 1, steps + 1)[1:]:
                            value = (1 - fraction) ** 3 * start + 3 * (1 - fraction) ** 2 * fraction * control1 + 3 * (1 - fraction) * fraction ** 2 * control2 + fraction ** 3 * end
                            points.append(value.tolist())
                        bezier = []
                    closed = closed or bool(raw.FPDFPathSegment_GetClose(segment))
                    continue
                if segment_type == raw.FPDF_SEGMENT_MOVETO and points:
                    paths.append((points, closed))
                    points, closed = [], False
                points.append(mapped)
                closed = closed or bool(raw.FPDFPathSegment_GetClose(segment))
            if points:
                paths.append((points, closed))
            # Compound fills may have winding holes. Keep their fill as local
            # artwork while extracting any ordinary editable stroke outlines.
            compound_fill = len(paths) > 1 and fill
            if compound_fill and not stroke:
                continue
            for points, closed in paths:
                if len(points) > 10000:
                    original = np.asarray(points, np.float32).reshape(-1, 1, 2)
                    for epsilon in (.05, .1, .25, .5, .75):
                        simplified = cv2.approxPolyDP(original, epsilon, closed).reshape(-1, 2).tolist()
                        if len(simplified) <= 10000:
                            points = simplified
                            break
                    if len(points) > 10000:
                        raise ValueError('도형의 경로가 너무 복잡해 안전하게 편집 가능한 객체로 변환할 수 없습니다. 해당 쪽을 나누거나 원본을 단순화해 주세요.')
                block = _shape(points, closed=closed, stroke=stroke, stroke_width=physical_width,
                               fill=None if compound_fill else fill, source='native-pdf-vector')
                if block and block.get('fill_color') and any(
                    table['x'] - 2 <= block['x'] and table['y'] - 2 <= block['y']
                    and block['x'] + block['width'] <= table['x'] + table['width'] + 2
                    and block['y'] + block['height'] <= table['y'] + table['height'] + 2
                    for table in tables):
                    continue
                if not block or _covered_by_table(block, table_mask):
                    continue
                if block.get('fill_color') == '#ffffff' and block['width'] >= width * .98 and block['height'] >= height * .98:
                    continue  # The white paper surface is implicit.
                shapes.append(block)
    walk(page, np.eye(3))
    return shapes, protected


def apply_native_table_styles(page, width, height, tables):
    """Prefer exact PDF path colors/widths over anti-aliased raster samples."""
    if not tables:
        return
    paths, _ = native_pdf_shapes(page, width, height, [])
    lines = []
    for block in paths:
        points = _points(block)
        if not block.get('stroke_color'):
            continue
        if block.get('closed') and len(points) > 2 and not np.array_equal(points[0], points[-1]):
            points = np.vstack((points, points[0]))
        for a, b in zip(points, points[1:]):
            line = dict(block, x=float(min(a[0], b[0])), y=float(min(a[1], b[1])),
                        width=float(abs(b[0] - a[0])), height=float(abs(b[1] - a[1])))
            lines.append((line, a, b))
    fills = [block for block in paths if block.get('fill_color') and block.get('closed')]
    for table in tables:
        tx, ty, tw, th = (table[key] for key in ('x', 'y', 'width', 'height'))
        nearby_lines = [(line, a, b) for line, a, b in lines
                        if line['x'] <= tx + tw + 5 and line['x'] + line['width'] >= tx - 5
                        and line['y'] <= ty + th + 5 and line['y'] + line['height'] >= ty - 5]
        for cell in table.get('cells', []):
            x, y, w, h = (cell[key] for key in ('x', 'y', 'width', 'height'))
            matches = []
            for line, a, b in nearby_lines:
                tolerance = max(3, line.get('stroke_width', 1) * 1.5)
                weight = 0
                side = None
                if abs(a[1] - b[1]) <= .5 and min(abs(a[1] - y), abs(a[1] - y - h)) <= tolerance:
                    side = 'top' if abs(a[1] - y) <= abs(a[1] - y - h) else 'bottom'
                    weight = max(0, min(max(a[0], b[0]), x + w) - max(min(a[0], b[0]), x))
                elif abs(a[0] - b[0]) <= .5 and min(abs(a[0] - x), abs(a[0] - x - w)) <= tolerance:
                    side = 'left' if abs(a[0] - x) <= abs(a[0] - x - w) else 'right'
                    weight = max(0, min(max(a[1], b[1]), y + h) - max(min(a[1], b[1]), y))
                if weight >= 2:
                    matches.append((line, weight, side))
            if matches:
                colors = {}
                for line, weight, _ in matches:
                    colors[line['stroke_color']] = colors.get(line['stroke_color'], 0) + weight
                cell['border_color'] = max(colors, key=colors.get)
                chosen = [(line, weight) for line, weight, _ in matches if line['stroke_color'] == cell['border_color']]
                cell['border_width'] = sum(line['stroke_width'] * weight for line, weight in chosen) / sum(weight for _, weight in chosen)
                borders = {}
                for side in ('left', 'right', 'top', 'bottom'):
                    edges = [(line, weight) for line, weight, edge in matches if edge == side]
                    if not edges:
                        borders[side] = {'color': cell.get('border_color', '#000000'), 'width': 0.}
                        continue
                    edge_colors = {}
                    for line, weight in edges:
                        edge_colors[line['stroke_color']] = edge_colors.get(line['stroke_color'], 0) + weight
                    color = max(edge_colors, key=edge_colors.get)
                    chosen = [(line, weight) for line, weight in edges if line['stroke_color'] == color]
                    borders[side] = {'color': color, 'width': sum(line['stroke_width'] * weight for line, weight in chosen) / sum(weight for _, weight in chosen)}
                cell['borders'] = borders
            for fill in reversed(fills):
                intersection = max(0, min(x + w, fill['x'] + fill['width']) - max(x, fill['x'])) * max(0, min(y + h, fill['y'] + fill['height']) - max(y, fill['y']))
                if intersection >= w * h * .85:
                    cell['fill_color'] = fill['fill_color']
                    break
        colors = [cell['border_color'] for cell in table.get('cells', []) if cell.get('border_color')]
        if colors:
            table['border_color'] = max(dict.fromkeys(colors), key=colors.count)
            table['border_width'] = float(np.median([cell.get('border_width', 1) for cell in table['cells']]))


def raster_form_shapes(image, texts, tables):
    """Vectorize long scanned rules after excluding recognized glyph regions."""
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    ink = (gray < 215).astype(np.uint8) * 255
    for block in texts + tables:
        x, y = max(0, int(block['x']) - 2), max(0, int(block['y']) - 2)
        r, b = min(width, int(math.ceil(block['x'] + block['width'])) + 2), min(height, int(math.ceil(block['y'] + block['height'])) + 2)
        ink[y:b, x:r] = 0
    result = []
    # Uniform colored form bars/boxes are editable fills rather than pictures.
    colored = (np.max(image, axis=2) - np.min(image, axis=2) > 18) & (gray < 248)
    keys = (image[:, :, 0].astype(np.uint16) // 32) * 64
    keys += (image[:, :, 1].astype(np.uint16) // 32) * 8
    keys += image[:, :, 2].astype(np.uint16) // 32
    candidate_keys, frequencies = np.unique(keys[colored], return_counts=True)
    for key, frequency in zip(candidate_keys, frequencies):
        if frequency < 180:
            continue
        mask = ((keys == key) & colored).astype(np.uint8) * 255
        for table in tables:
            x, y = max(0, int(table['x'])), max(0, int(table['y']))
            r, b = min(width, int(table['x'] + table['width']) + 1), min(height, int(table['y'] + table['height']) + 1)
            mask[y:b, x:r] = 0
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w < 20 or h < 10 or w * h < 250:
                continue
            if np.count_nonzero(mask[y:y + h, x:x + w]) < w * h * .87:
                continue
            result.append(_shape([[x, y], [x + w - 1, y], [x + w - 1, y + h - 1], [x, y + h - 1]],
                                 closed=True, stroke=None, fill=_dominant_color(image[y:y + h, x:x + w])))
    for horizontal in (True, False):
        minimum = max(30, int((width if horizontal else height) * .025))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (minimum, 1) if horizontal else (1, minimum))
        opened = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            length, thickness = (w, h) if horizontal else (h, w)
            if length < minimum or thickness > max(12, length * .1):
                continue
            points = [[x, y + (h - 1) / 2], [x + w - 1, y + (h - 1) / 2]] if horizontal else [[x + (w - 1) / 2, y], [x + (w - 1) / 2, y + h - 1]]
            result.append(_shape(points, stroke=_dominant_color(image[y:y + h, x:x + w][opened[y:y + h, x:x + w] > 0], '#000000'), stroke_width=thickness))
    # Diagonal form rules (crosses, slashes and diagram connectors) must be
    # vectors too; orthogonal morphology cannot detect them.
    candidates = cv2.HoughLinesP(ink, 1, np.pi / 720, threshold=max(25, int(min(width, height) * .08)),
                                minLineLength=max(35, int(min(width, height) * .12)), maxLineGap=6)
    diagonals = []
    if candidates is not None:
        ordered = sorted(candidates[:, 0], key=lambda p: -(float(p[2] - p[0]) ** 2 + float(p[3] - p[1]) ** 2))
        for x0, y0, x1, y1 in ordered:
            if abs(x1 - x0) < 5 or abs(y1 - y0) < 5:
                continue
            a, b = np.asarray([x0, y0], float), np.asarray([x1, y1], float)
            direction = (b - a) / np.linalg.norm(b - a)
            normal = np.asarray([-direction[1], direction[0]])
            left, right = max(0, min(x0, x1) - 5), min(width, max(x0, x1) + 6)
            top, bottom = max(0, min(y0, y1) - 5), min(height, max(y0, y1) + 6)
            if np.count_nonzero(ink[top:bottom, left:right]) > (right - left) * (bottom - top) * .4:
                continue  # Dense photograph interiors are not form rules.
            if any(abs(float(np.dot(b - a, old_normal))) < np.linalg.norm(b - a) * .025
                   and abs(float(np.dot((a + b) / 2 - old_a, old_normal))) <= 5
                   for old_a, old_normal in diagonals):
                continue
            widths = []
            samples = []
            for fraction in np.linspace(.05, .95, 9):
                center = a + (b - a) * fraction
                points = np.rint(center + np.arange(-8, 9)[:, None] * normal).astype(int)
                points[:, 0] = np.clip(points[:, 0], 0, width - 1)
                points[:, 1] = np.clip(points[:, 1], 0, height - 1)
                marked = ink[points[:, 1], points[:, 0]] > 0
                widths.append(np.count_nonzero(marked))
                samples.extend(image[points[marked, 1], points[marked, 0]].tolist())
            thickness = max(1, float(np.median(widths)))
            if thickness > 10:
                continue
            diagonals.append((a, normal))
            result.append(_shape([a, b], stroke=_dominant_color(samples, '#000000'), stroke_width=thickness))
    return [block for block in result if block]


def extract_editable_graphics(image, texts, tables, source: Path, *, native_shapes=None, protected_rects=()):
    """Return editable rules/shapes plus tight crops of remaining real graphics."""
    source = Path(source)
    height, width = image.shape[:2]
    sample_table_styles(image, tables)
    if native_shapes is None:
        shapes = raster_form_shapes(image, texts, tables)
    else:
        shapes = list(native_shapes)
        # Searchable scanned PDFs have native text but raster-only form rules.
        # Supplement exact paths from residual source pixels without duplicating
        # those paths or vectorizing the interiors of actual local photographs.
        candidate = image.copy()
        candidate_mask = np.zeros((height, width), np.uint8)
        for block in shapes:
            _paint_mask(candidate_mask, block, extra=3, reference=image)
        for box in protected_rects:
            x, y = max(0, int(box['x'])), max(0, int(box['y']))
            r, b = min(width, int(math.ceil(box['x'] + box['width']))), min(height, int(math.ceil(box['y'] + box['height'])))
            candidate_mask[y:b, x:r] = 255
        candidate[candidate_mask > 0] = 255
        shapes.extend(raster_form_shapes(candidate, texts, tables))
    residual = image.copy()
    remove = table_boundary_mask(image.shape, tables, 5)
    preserved_artwork = np.zeros_like(remove)
    text_mask = np.zeros((height, width), np.uint8)
    for block in texts:
        x, y = max(0, int(block['x']) - 1), max(0, int(block['y']) - 1)
        r, b = min(width, int(math.ceil(block['x'] + block['width'])) + 1), min(height, int(math.ceil(block['y'] + block['height'])) + 1)
        text_mask[y:b, x:r] = 255
    # Text extraction has already finished: this affects picture crops only.
    residual[text_mask > 0] = 255
    protected = np.zeros_like(remove)
    for box in protected_rects:
        x, y = max(0, int(box['x'])), max(0, int(box['y']))
        r, b = min(width, int(math.ceil(box['x'] + box['width']))), min(height, int(math.ceil(box['y'] + box['height'])))
        protected[y:b, x:r] = 255
    for shape in shapes:
        _paint_mask(remove, shape, extra=3, reference=image, preserve=preserved_artwork)
    for table in tables:
        for cell in table.get('cells', []):
            x, y = max(0, int(cell['x'])), max(0, int(cell['y']))
            r, b = min(width, int(math.ceil(cell['x'] + cell['width']))), min(height, int(math.ceil(cell['y'] + cell['height'])))
            if r <= x or b <= y:
                continue
            fill_bgr = np.asarray(_rgb(cell.get('fill_color', '#ffffff'))[::-1])
            roi = residual[y:b, x:r]
            matches = np.max(np.abs(roi.astype(np.int16) - fill_bgr), axis=2) <= 24
            remove[y:b, x:r][matches] = 255
            preserved_artwork[y:b, x:r][~matches] = 255
            # Genuine cell text is fully represented by the editable table.
            for text in cell.get('text_blocks', []):
                tx, ty = max(x, int(text['x']) - 1), max(y, int(text['y']) - 1)
                tr, tb = min(r, int(math.ceil(text['x'] + text['width'])) + 1), min(b, int(math.ceil(text['y'] + text['height'])) + 1)
                remove[ty:tb, tx:tr] = 255
    remove[protected > 0] = 0
    residual[remove > 0] = 255
    preserved_artwork[(remove > 0) | (text_mask > 0)] = 0
    gray = cv2.cvtColor(residual, cv2.COLOR_BGR2GRAY)
    ink = (gray < 245).astype(np.uint8) * 255
    ink[preserved_artwork > 0] = 255
    # Group nearby genuine picture ink; do not add a whole-page fallback.
    grouped = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
    contours, _ = cv2.findContours(grouped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    pictures = []
    for contour in sorted(contours, key=lambda c: (cv2.boundingRect(c)[1], cv2.boundingRect(c)[0])):
        x, y, w, h = cv2.boundingRect(contour)
        pixels = int(np.count_nonzero(ink[y:y + h, x:x + w]))
        if pixels < 24 or w < 3 or h < 3:
            continue
        yy, xx = np.where(ink[y:y + h, x:x + w] > 0)
        left, top = max(0, x + int(xx.min()) - 1), max(0, y + int(yy.min()) - 1)
        right, bottom = min(width, x + int(xx.max()) + 2), min(height, y + int(yy.max()) + 2)
        if right - left >= width * .97 and bottom - top >= height * .97:
            region = residual[top:bottom, left:right]
            gray_region = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
            histogram = np.bincount((gray_region // 16).ravel(), minlength=16).astype(float)
            probabilities = histogram[histogram > 0] / histogram.sum()
            entropy = -float(np.sum(probabilities * np.log2(probabilities)))
            color_fraction = float(np.mean(np.max(region, axis=2) - np.min(region, axis=2) > 30))
            ink_fraction = float(np.mean(gray_region < 240))
            genuine_photo = entropy > 2.2 and ink_fraction > .45 and (color_fraction > .08 or np.std(gray_region) > 40)
            if not genuine_photo:
                raise ValueError('페이지 전체에 남은 양식 그림을 편집 가능한 도형으로 분리하지 못했습니다. 원본을 더 선명하게 스캔하거나 다른 인식 방식을 선택해 주세요.')
            source_kind = 'full-page-photo'
        else:
            source_kind = 'localized-graphic'
        name = f'{source.stem}-graphic-{len(pictures) + 1}.png'
        # Crops can overlap an editable vector. Transparent removed pixels keep
        # an image's rectangular white margins from hiding that vector again.
        rgba = cv2.cvtColor(residual[top:bottom, left:right], cv2.COLOR_BGR2RGBA)
        transparent = (remove[top:bottom, left:right] > 0) | (text_mask[top:bottom, left:right] > 0)
        rgba[:, :, 3][transparent] = 0
        Image.fromarray(rgba).save(source.parent / name)
        pictures.append({'type': 'image', 'x': left, 'y': top, 'width': right - left, 'height': bottom - top,
                         'image_url': f'/api/assets/{source.parent.name}/{name}', 'role': 'picture', 'source': source_kind})
    return shapes + pictures


def upgrade_legacy_page(page, asset_root, document_id=None):
    """Upgrade cached background-image pages without changing text or table edits.

    New PDF conversions use exact native vectors. A saved project with only
    raster assets can still expose its long form rules as editable geometry.
    """
    backgrounds = [block for block in page.get('blocks', [])
                   if block.get('type') == 'image' and block.get('role') == 'graphics-background']
    if not backgrounds:
        return page
    from copy import deepcopy
    from .hwpx import _asset_path
    updated = deepcopy(page)
    full = next((block for block in backgrounds if block.get('width', 0) >= page.get('width', 1) * .9
                 and block.get('height', 0) >= page.get('height', 1) * .9), None)
    # Native legacy assets already removed glyph ink precisely; retain that
    # separation rather than rediscovering native cell text from a screenshot.
    source_url = (full.get('image_url') if full else page.get('image_url')) or backgrounds[0].get('image_url')
    if full:
        viewport = {key: full.get(key, 0) for key in ('x', 'y', 'width', 'height')}
    elif page.get('image_url'):
        viewport = {'x': 0, 'y': 0, 'width': page['width'], 'height': page['height']}
    else:
        viewport = {key: backgrounds[0].get(key, 0) for key in ('x', 'y', 'width', 'height')}
    if any(not math.isfinite(float(viewport[key])) for key in viewport) or viewport['width'] <= 0 or viewport['height'] <= 0:
        raise ValueError('기존 그림 배경의 좌표가 올바르지 않습니다. 원본 파일을 다시 변환해 주세요.')
    source = _asset_path(source_url, Path(asset_root), document_id)
    with Image.open(source) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError('기존 그림 배경이 너무 커서 안전하게 편집 가능한 양식으로 바꿀 수 없습니다. 원본 파일을 다시 변환해 주세요.')
        rgb = np.asarray(image.convert('RGB'))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    sx, sy = rgb.shape[1] / viewport['width'], rgb.shape[0] / viewport['height']
    scale = math.sqrt(sx * sy)
    retained = [block for block in updated['blocks'] if block.get('role') != 'graphics-background']
    texts = [block for block in retained if block.get('type') == 'text']
    tables = [block for block in retained if block.get('type') == 'table']
    style_names = ('fill_color', 'border_color', 'border_width', 'borders')
    def to_asset(record):
        mapped = deepcopy(record)
        mapped['x'] = (record['x'] - viewport['x']) * sx
        mapped['y'] = (record['y'] - viewport['y']) * sy
        mapped['width'], mapped['height'] = record['width'] * sx, record['height'] * sy
        for key in ('font_size', 'border_width', 'stroke_width'):
            if key in mapped:
                mapped[key] *= scale
        for key in ('cells', 'text_blocks'):
            mapped[key] = [to_asset(child) for child in record.get(key, [])]
        return mapped
    mapped_tables = [to_asset(table) for table in tables]
    mapped_texts = [to_asset(text) for text in texts]
    mapped_texts.extend(to_asset(block) for block in retained if block.get('type') == 'image')
    mapped_texts = [block for block in mapped_texts if block['x'] < rgb.shape[1] and block['y'] < rgb.shape[0]
                    and block['x'] + block['width'] > 0 and block['y'] + block['height'] > 0]
    graphics = extract_editable_graphics(bgr, mapped_texts, mapped_tables, source)
    # Only fill previously absent styles; explicit user edits are authoritative.
    for original, detected in zip(tables, mapped_tables):
        for key in style_names:
            if key in detected and key not in original:
                original[key] = detected[key] / scale if key == 'border_width' else deepcopy(detected[key])
        for original_cell, detected_cell in zip(original.get('cells', []), detected.get('cells', [])):
            for key in style_names:
                if key in detected_cell and key not in original_cell:
                    original_cell[key] = detected_cell[key] / scale if key == 'border_width' else deepcopy(detected_cell[key])
    for index, block in enumerate(graphics):
        block['id'] = f'{source.stem}-legacy-graphic-{index + 1}'
        block['x'], block['y'] = viewport['x'] + block['x'] / sx, viewport['y'] + block['y'] / sy
        block['width'], block['height'] = block['width'] / sx, block['height'] / sy
        if block.get('points'):
            block['points'] = [[x / sx, y / sy] for x, y in block['points']]
        if 'stroke_width' in block:
            block['stroke_width'] /= scale
    updated['blocks'] = retained + graphics
    updated['graphics_migration'] = 'editable-raster-form-and-local-pictures'
    return updated
