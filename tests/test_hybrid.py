from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from converter import ocr

ROOT = Path(__file__).resolve().parents[1]


class HybridReviewTests(unittest.TestCase):
    def run_hybrid(self, proposal=None, error=None, original="예산: 1,000원 / AI"):
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build) as folder:
            source = Path(folder) / "source.png"
            Image.new("RGB", (1000, 2000), "white").save(source)
            blocks = [{"type": "text", "x": 20, "y": 50, "width": 300, "height": 40, "text": original, "confidence": .70, "font_size": 25},
                      {"type": "table", "x": 20, "y": 150, "width": 400, "height": 100, "rows": [["수량", "120명"]], "confidence": .90}]
            proposed = [{"type": "text", "text": proposal or blocks[0]["text"], "x": 0, "y": 0, "width": 300, "height": 40}]
            with patch.object(ocr, "_recognize_local", return_value=(blocks, [])), patch.object(ocr, "_recognize_api", return_value=(proposed, []), side_effect=error) as model:
                result = ocr.recognize_page(source, "sllm", {})
            return result, model.call_args

    def test_matching_review_keeps_layout_text_and_confidence(self):
        result, call = self.run_hybrid()
        block = result["blocks"][0]
        self.assertEqual(block["text"], "예산: 1,000원 / AI")
        self.assertEqual((block["x"], block["y"], block["width"], block["height"]), (20, 50, 300, 40))
        self.assertEqual(block["confidence"], .70)
        self.assertEqual(block["model_review"]["status"], "confirmed")
        self.assertEqual(result["blocks"][1]["rows"], [["수량", "120명"]])
        self.assertEqual(call.args[3]["api_max_tokens"], 512)
        self.assertEqual(call.args[3]["base_url"], "http://127.0.0.1:18081/v1")

    def test_changed_number_is_suggestion_and_never_applied(self):
        result, _ = self.run_hybrid("예산: 9,000원 / AI")
        block = result["blocks"][0]
        self.assertEqual(block["text"], "예산: 1,000원 / AI")
        self.assertNotIn("suggested_text", block)
        self.assertFalse(block["model_review"]["numbers_preserved"])
        self.assertEqual(block["model_review"]["status"], "failed")

    def test_model_failure_returns_ocr_success(self):
        result, _ = self.run_hybrid(error=RuntimeError("incomplete JSON"))
        self.assertEqual(result["engine"], "sllm")
        self.assertEqual(result["blocks"][0]["text"], "예산: 1,000원 / AI")
        self.assertEqual(result["blocks"][0]["model_review"]["status"], "failed")
        self.assertEqual(result["reviewed_lines"], 0)
        self.assertEqual(result["review_attempts"], 1)
        self.assertTrue(any("기본 한국어 OCR" in warning for warning in result["warnings"]))

    def test_dropped_number_sign_or_parentheses_blocks_suggestion(self):
        for source in ("증감: -10명", "증감: +10명", "증감: −10명", "증감: (10)명"):
            with self.subTest(source=source):
                result, _ = self.run_hybrid("증감: 10명", original=source)
                block = result["blocks"][0]
                self.assertEqual(block["text"], source)
                self.assertFalse(block["model_review"]["numbers_preserved"])
                self.assertEqual(block["model_review"]["status"], "failed")
                self.assertNotIn("suggested_text", block)

    def test_unrelated_model_text_is_not_presented_as_a_suggestion(self):
        result, _ = self.run_hybrid("전혀 관계없는 허위 문장입니다")
        block = result["blocks"][0]
        self.assertEqual(block["text"], "예산: 1,000원 / AI")
        self.assertEqual(block["model_review"]["status"], "failed")
        self.assertNotIn("suggested_text", block)
        self.assertEqual(result["reviewed_lines"], 0)


if __name__ == "__main__":
    unittest.main()
