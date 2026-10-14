"""Bridge Pillow-validated BMP raw layouts to libvips without full decoding.

Pillow parses the BMP header/decoder layout. libvips rawload maps rows and
bandfold/bandunfold remove padding or unpack indexed pixels in its pipeline.
https://www.libvips.org/API/8.17/ctor.Image.rawload.html
"""
from pathlib import Path
from PIL import Image


def load_bmp(pyvips, path):
    path = Path(path)
    from .large_raster import _HEADER_LOCK
    with _HEADER_LOCK:
        previous_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None  # Header only; native raw reader never calls Pillow.load.
        try:
            with Image.open(path) as source:
                if source.format != 'BMP' or len(source.tile) != 1:
                    raise ValueError('이 큰 BMP는 분할을 지원하지 않아요. 그림판에서 PNG로 다시 저장한 뒤 선택해 주세요.')
                decoder, extent, offset, args = source.tile[0]
                if decoder != 'raw' or extent != (0, 0, source.width, source.height):
                    raise ValueError('이 큰 BMP의 압축 방식은 지원하지 않아요. PNG로 다시 저장해 주세요.')
                mode, stride, direction = args
                width, height = source.size
                palette = source.palette.getdata() if source.palette else None
        finally:
            Image.MAX_IMAGE_PIXELS = previous_limit
    if direction not in {-1, 1} or stride <= 0 or offset < 0 or offset + stride * height > path.stat().st_size:
        raise ValueError('BMP의 화소 자료가 손상되었어요. 원본 파일을 확인해 주세요.')
    image = pyvips.Image.rawload(str(path), stride, height, 1, offset=offset,
                                format='uchar', access='random')
    if direction == -1:
        image = image.flipver()
    if mode in {'BGR', 'RGB', 'BGRX', 'RGBX', 'BGRA', 'RGBA'}:
        bands = len(mode)
        if width * bands > stride:
            raise ValueError('BMP 행 크기가 올바르지 않아요.')
        image = image.crop(0, 0, width * bands, height).bandfold(factor=bands)
        indices = [2, 1, 0] if mode.startswith('BGR') else [0, 1, 2]
        if mode.endswith('A'):
            indices.append(3)
        return image[indices].copy(interpretation='srgb')
    if mode in {'1', '1;I', 'P;1'}:
        planes = [((image >> shift) & 1).cast('uchar') for shift in range(7, -1, -1)]
        image = planes[0].bandjoin(planes[1:]).bandunfold(factor=8).crop(0, 0, width, height)
        if mode == '1;I':
            image = 1 - image
        if mode.startswith('1'):
            return (image * 255).cast('uchar').copy(interpretation='b-w')
    elif mode in {'P', 'L'}:
        image = image.crop(0, 0, width, height)
        if mode == 'L':
            return image.copy(interpretation='b-w')
    else:
        raise ValueError('이 큰 BMP는 PNG로 다시 저장한 뒤 변환해 주세요.')
    if not palette:
        raise ValueError('BMP 색상표를 찾을 수 없어요.')
    palette_mode, values = palette
    if palette_mode not in {'RGB', 'BGR', 'BGRX', 'RGBA', 'BGRA'}:
        raise ValueError('이 BMP 색상표는 지원하지 않아요. PNG로 저장해 주세요.')
    step = len(palette_mode)
    colors = bytearray()
    for i in range(0, len(values) - step + 1, step):
        entry = values[i:i + step]
        colors.extend(entry[2::-1] if palette_mode.startswith('BGR') else entry[:3])
    colors = (colors + bytes(768))[:768]
    lut = pyvips.Image.new_from_memory(bytes(colors), 256, 1, 3, 'uchar')
    return image.maplut(lut).copy(interpretation='srgb')
