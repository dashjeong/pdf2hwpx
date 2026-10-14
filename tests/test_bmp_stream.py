from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from converter.runtime_bootstrap import configure_native_runtime
configure_native_runtime()
import pyvips
pyvips.cache_set_max(0)
from converter.bmp_stream import load_bmp

ROOT = Path(__file__).resolve().parents[1] / 'tmp' / 'bmp-stream-tests'
ROOT.mkdir(parents=True, exist_ok=True)


class BmpStreamTests(unittest.TestCase):
    def test_rgb_padding_grayscale_palette_and_one_bit_match_pillow(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for mode in ('RGB', 'L', 'P', '1'):
                image = Image.new(mode, (97, 61))
                if mode == 'P':
                    image.putpalette([value for i in range(256) for value in (i, 255-i, i//2)])
                image.putdata([(i % 256, (i*3) % 256, (i*7) % 256) if mode == 'RGB' else
                               (i % 2) * 255 if mode == '1' else i % 256 for i in range(97*61)])
                path = Path(directory) / f'{mode}.bmp'
                image.save(path)
                with Image.open(path) as original:
                    expected = original.convert('RGB').tobytes()
                with patch.object(Image.Image, 'load', side_effect=AssertionError('full Pillow decode')):
                    loaded = load_bmp(pyvips, path)
                    if loaded.interpretation != 'srgb':
                        loaded = loaded.colourspace('srgb')
                    self.assertEqual(bytes(loaded.write_to_memory()), expected, mode)
                    del loaded

    def test_top_down_rows_and_truncated_payload(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'source.bmp'
            image = Image.new('RGB', (13, 7))
            image.putdata([(i*11 % 256, i*3 % 256, i*7 % 256) for i in range(91)])
            image.save(path)
            data = bytearray(path.read_bytes())
            with Image.open(path) as parsed:
                offset, stride = parsed.tile[0][2], parsed.tile[0][3][1]
            data[offset:offset+stride*7] = b''.join(data[offset+i*stride:offset+(i+1)*stride] for i in reversed(range(7)))
            struct.pack_into('<i', data, 22, -7)
            path.write_bytes(data)
            self.assertEqual(bytes(load_bmp(pyvips, path).write_to_memory()), image.tobytes())
            path.write_bytes(data[:-1])
            with self.assertRaisesRegex(ValueError, '손상'):
                load_bmp(pyvips, path)
