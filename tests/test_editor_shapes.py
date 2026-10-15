"""Run real JavaScript editor state regressions without a browser/model."""
from pathlib import Path
import shutil
import subprocess
import unittest


class EditorShapeTests(unittest.TestCase):
    def test_shapes_styles_and_edited_native_text_round_trip(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is needed for the JavaScript DOM harness')
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, str(root / 'tests' / 'test_editor_shapes.cjs')],
                                cwd=root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('regressions passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
