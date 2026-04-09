import unittest
from unittest.mock import patch

from utils import translation


class TestTranslateTextFlow(unittest.TestCase):
    def test_empty_paragraphs_returns_empty_list(self):
        result = translation.translate_text_with_gemini([])
        self.assertEqual(result, [])

    def test_uses_5_retries_for_small_batch(self):
        paragraphs = ["a", "b", "c"]

        with patch(
            "utils.translation._translate_text_batch_with_retry",
            return_value=["ja-a", "ja-b", "ja-c"],
        ) as mock_batch:
            result = translation.translate_text_with_gemini(paragraphs, model_name="m")

        self.assertEqual(result, ["ja-a", "ja-b", "ja-c"])
        mock_batch.assert_called_once_with(paragraphs, model_name="m", max_retries=5)

    def test_uses_3_retries_for_large_batch(self):
        paragraphs = [f"p{i}" for i in range(80)]
        translated = [f"ja-{i}" for i in range(80)]

        with patch(
            "utils.translation._translate_text_batch_with_retry",
            return_value=translated,
        ) as mock_batch:
            result = translation.translate_text_with_gemini(paragraphs, model_name="m")

        self.assertEqual(result, translated)
        mock_batch.assert_called_once_with(paragraphs, model_name="m", max_retries=3)

    def test_splits_and_merges_when_batch_retries_exhausted(self):
        paragraphs = [f"p{i}" for i in range(6)]

        def fake_batch(sub_paragraphs, model_name, max_retries):
            # Force split on the original batch, then succeed on child batches.
            if len(sub_paragraphs) == 6:
                raise RuntimeError("Failed to execute call_gemini_api after retries")
            return [f"ja-{p}" for p in sub_paragraphs]

        with patch(
            "utils.translation._translate_text_batch_with_retry",
            side_effect=fake_batch,
        ) as mock_batch:
            result = translation.translate_text_with_gemini(paragraphs, model_name="m")

        self.assertEqual(result, [f"ja-p{i}" for i in range(6)])

        # 1st call fails at len=6, then split calls len=3 and len=3.
        call_lengths = [len(call.args[0]) for call in mock_batch.call_args_list]
        self.assertEqual(call_lengths, [6, 3, 3])

    def test_raises_when_single_paragraph_keeps_failing(self):
        with patch(
            "utils.translation._translate_text_batch_with_retry",
            side_effect=RuntimeError("Failed to execute call_gemini_api after retries"),
        ):
            with self.assertRaises(RuntimeError):
                translation.translate_text_with_gemini(["only-one"], model_name="m")


if __name__ == "__main__":
    unittest.main()
