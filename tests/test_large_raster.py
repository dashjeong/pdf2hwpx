from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from collections import namedtuple
import json
import os
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from converter.large_raster import raster_header, tile_plan, prepare_raster_tiles, _cleanup_owned_workspace

ROOT = Path(__file__).resolve().parents[1]


class LargeRasterTests(unittest.TestCase):
    def temporary(self):
        (ROOT / "build").mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(dir=ROOT / "build")

    def test_cores_cover_each_pixel_once_and_tiles_obey_budget(self):
        coverage = np.zeros((77, 103), np.uint8)
        for tile in tile_plan(103, 77, 1024):
            x0, y0, x1, y1 = tile["core"]
            coverage[y0:y1, x0:x1] += 1
            self.assertLessEqual(tile["width"] * tile["height"], 1024)
            self.assertLessEqual(tile["x"], x0)
            self.assertLessEqual(tile["y"], y0)
            self.assertGreaterEqual(tile["x"] + tile["width"], x1)
            self.assertGreaterEqual(tile["y"] + tile["height"], y1)
        self.assertTrue(np.all(coverage == 1))

    def test_png_header_and_exif_do_not_decode_pixels(self):
        with self.temporary() as folder:
            path = Path(folder) / "source.png"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (90, 40), "white").save(path, exif=exif)
            with patch.object(Image.Image, "load", side_effect=AssertionError("Full decode in parent")):
                header = raster_header(path)
            self.assertEqual((header["width"], header["height"]), (40, 90))
            self.assertEqual(header["orientation"], 6)

    def test_png_physical_dpi_is_read_without_pixel_decode_and_axes_rotate(self):
        with self.temporary() as temporary:
            path = Path(temporary) / 'dpi.png'
            exif = Image.Exif()
            exif[274] = 6
            Image.new('RGB', (90, 40), 'white').save(path, dpi=(150, 300), exif=exif)
            with patch.object(Image.Image, 'load', side_effect=AssertionError('Full decode')):
                header = raster_header(path)
            self.assertAlmostEqual(header['dpi'], 300, delta=.02)
            self.assertEqual((header['width'], header['height']), (40, 90))

    def test_png_missing_resolution_uses_existing_a4_estimate(self):
        with self.temporary() as temporary:
            path = Path(temporary) / 'no-dpi.png'
            Image.new('RGB', (2140, 1200), 'white').save(path)
            self.assertAlmostEqual(raster_header(path)['dpi'], 2140 / 11.6929, places=5)

    def test_webp_header_avoids_pillow_anim_decoder(self):
        with self.temporary() as folder:
            path = Path(folder) / "source.webp"
            Image.new("RGB", (90, 40), "white").save(path, lossless=True)
            with patch.object(Image, "open", side_effect=AssertionError("Pillow WebP decode")):
                header = raster_header(path)
            self.assertEqual((header["width"], header["height"]), (90, 40))

    def test_isolated_exif_tiles_match_source_and_remove_spool(self):
        with self.temporary() as folder:
            folder = Path(folder)
            path = folder / "source.jpg"
            image = Image.new("RGB", (160, 96), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 79, 95), fill="red")
            draw.rectangle((80, 0, 159, 95), fill="green")
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, quality=100, subsampling=0, exif=exif)
            with Image.open(path) as image:
                expected = np.asarray(ImageOps.exif_transpose(image).convert("RGB")).astype(int)
            result = prepare_raster_tiles(path, folder, 0, 4096, 512 * 1024 * 1024)
            self.assertEqual((result["width"], result["height"]), (96, 160))
            self.assertGreater(len(result["tiles"]), 1)
            for tile in result["tiles"]:
                with Image.open(tile["path"]) as image:
                    observed = np.asarray(image).astype(int)
                y, x, h, w = tile["y"], tile["x"], tile["height"], tile["width"]
                self.assertLessEqual(int(np.abs(observed - expected[y:y + h, x:x + w]).max()), 2)
            self.assertFalse(list(folder.glob(".raster-*")))
            with Image.open(result["image_path"]) as preview:
                self.assertLessEqual(preview.width * preview.height, 4096)

    def test_insufficient_disk_or_budget_fails_before_spawning(self):
        with self.temporary() as folder:
            folder = Path(folder)
            path = folder / "source.png"
            Image.new("RGB", (90, 40), "white").save(path)
            with self.assertRaisesRegex(ValueError, "메모리"):
                prepare_raster_tiles(path, folder, 0, 4096, 32 * 1024 * 1024)
            Disk = namedtuple("Disk", "total used free")
            with patch("converter.large_raster.shutil.disk_usage", return_value=Disk(100, 100, 0)), \
                    patch("converter.large_raster.subprocess.Popen", side_effect=AssertionError("Spawned unexpectedly")):
                with self.assertRaisesRegex(RuntimeError, "임시 저장 공간"):
                    prepare_raster_tiles(path, folder, 0, 4096, 512 * 1024 * 1024)

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows file-sharing conflict')
    def test_cleanup_retries_actual_locked_file_after_handle_closes(self):
        with self.temporary() as temporary:
            folder = Path(temporary)
            work = folder / '.raster-owned'
            work.mkdir()
            handle = (work / 'stderr.txt').open('wb')
            def release():
                time.sleep(.2)
                handle.close()
            thread = threading.Thread(target=release)
            thread.start()
            try:
                cleaned, error = _cleanup_owned_workspace(work, folder, 'raster-owned')
            finally:
                thread.join()
            self.assertTrue(cleaned, error)
            self.assertFalse(work.exists())

    def prepared_fake_worker(self, source, folder, failed=False):
        class Process:
            pid, returncode = 12345, None
            def __init__(self, command, **kwargs):
                self.manifest = json.loads(Path(command[-1]).read_text(encoding='utf-8'))
            def wait(self, timeout=None):
                if self.returncode is None:
                    result = {'error': 'original decoder failure', 'diagnostic': {'stage': 'spool_rgb'}} if failed else {}
                    if not failed:
                        paths = [folder / 'prepared-preview.png', folder / 'prepared-tile.png']
                        for path in paths:
                            Image.new('RGB', (16, 16), 'white').save(path)
                        result.update(image_path=str(paths[0]), tiles=[{'path': str(paths[1])}], warnings=[])
                    Path(self.manifest['result_path']).write_text(json.dumps(result), encoding='utf-8')
                    self.returncode = 1 if failed else 0
                return self.returncode
            def poll(self):
                return self.returncode
        with patch('converter.large_raster.subprocess.Popen', side_effect=Process), \
             patch('converter.process_ownership.ProcessJob'), \
             patch('converter.large_raster._cleanup_owned_workspace', return_value=(False, 'sharing conflict')):
            return prepare_raster_tiles(source, folder, 0, 4096, 512 * 1024 ** 2)

    def test_locked_cleanup_preserves_valid_output_with_warning(self):
        with self.temporary() as temporary:
            folder = Path(temporary)
            source = folder / 'source.png'
            Image.new('RGB', (16, 16), 'white').save(source)
            result = self.prepared_fake_worker(source, folder)
            self.assertTrue(Path(result['image_path']).is_file())
            self.assertTrue(result['warnings'])
            lifecycle = json.loads(next(folder.glob('*-lifecycle.json')).read_text(encoding='utf-8'))
            self.assertTrue(lifecycle['child_exit_confirmed'])
            self.assertEqual(lifecycle['phase'], 'outputs_validated')
            self.assertFalse(lifecycle['cleanup_complete'])

    def test_locked_cleanup_does_not_replace_original_decoder_failure(self):
        with self.temporary() as temporary:
            folder = Path(temporary)
            source = folder / 'source.png'
            Image.new('RGB', (16, 16), 'white').save(source)
            with self.assertRaisesRegex(RuntimeError, 'original decoder failure'):
                self.prepared_fake_worker(source, folder, failed=True)
            lifecycle = json.loads(next(folder.glob('*-lifecycle.json')).read_text(encoding='utf-8'))
            self.assertEqual(lifecycle['worker_diagnostic']['stage'], 'spool_rgb')
            self.assertEqual(lifecycle['error'], 'original decoder failure')
            self.assertFalse(lifecycle['cleanup_complete'])


if __name__ == "__main__":
    unittest.main()
