import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from PIL import Image

from utils.docx_parser import parse_docx_with_images
from utils.image_utils import _try_cli_conversion, load_or_convert_image
from utils.metrics import MetricsCollector, NullSink
from utils.translation import ImageTranslation, QuotaExhaustedError, translate_image_with_gemini
from utils.translation_runner import translate_chunks_parallel, translate_chunks_sequential


def _create_sample_png_bytes() -> bytes:
    img = Image.new("RGB", (30, 30), color="blue")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestImageUtils(unittest.TestCase):
    def test_load_or_convert_empty_bytes(self):
        self.assertIsNone(load_or_convert_image(b""))
        self.assertIsNone(load_or_convert_image(None))

    def test_load_valid_png_bytes(self):
        png_bytes = _create_sample_png_bytes()
        img = load_or_convert_image(png_bytes)
        self.assertIsNotNone(img)
        self.assertEqual(img.size, (30, 30))

    @patch("utils.image_utils.shutil.which", return_value=None)
    def test_load_corrupted_bytes_no_converter_returns_none(self, mock_which):
        corrupted_bytes = b"NOT_A_VALID_IMAGE_DATA_12345"
        img = load_or_convert_image(corrupted_bytes)
        self.assertIsNone(img)

    @patch("utils.image_utils.shutil.which")
    @patch("utils.image_utils.subprocess.run")
    def test_try_cli_conversion_wmf2gd(self, mock_run, mock_which):
        # Setup mock for wmf2gd
        def which_side_effect(cmd):
            return "/usr/bin/wmf2gd" if cmd == "wmf2gd" else None

        mock_which.side_effect = which_side_effect

        def fake_run(cmd, capture_output=True, timeout=15):
            # cmd is: ["wmf2gd", "-t", "png", "-o", output_png, input_path]
            output_png = cmd[4]
            sample_png = Image.new("RGB", (50, 50), color="red")
            sample_png.save(output_png, format="PNG")
            mock_res = MagicMock()
            mock_res.returncode = 0
            return mock_res

        mock_run.side_effect = fake_run

        fake_wmf_bytes = b"\xd7\xcd\xc6\x9a" + b"\x00" * 30
        result = _try_cli_conversion(fake_wmf_bytes, suffix=".wmf")
        self.assertIsNotNone(result)
        self.assertEqual(result.size, (50, 50))

    @patch("utils.image_utils.shutil.which")
    @patch("utils.image_utils.subprocess.run")
    def test_try_cli_conversion_magick_fallback(self, mock_run, mock_which):
        def which_side_effect(cmd):
            if cmd == "wmf2gd":
                return None
            if cmd == "magick":
                return "/usr/bin/magick"
            return None

        mock_which.side_effect = which_side_effect

        def fake_run(cmd, capture_output=True, timeout=15):
            output_png = cmd[2]
            sample_png = Image.new("RGB", (40, 40), color="green")
            sample_png.save(output_png, format="PNG")
            mock_res = MagicMock()
            mock_res.returncode = 0
            return mock_res

        mock_run.side_effect = fake_run

        fake_wmf_bytes = b"\xd7\xcd\xc6\x9a" + b"\x00" * 30
        result = _try_cli_conversion(fake_wmf_bytes, suffix=".wmf")
        self.assertIsNotNone(result)
        self.assertEqual(result.size, (40, 40))


class TestTranslateImageWithGemini(unittest.TestCase):
    def test_none_image_returns_empty_list(self):
        result = translate_image_with_gemini(None)
        self.assertEqual(result, [])

    @patch("utils.translation._get_client")
    def test_successful_translation(self, mock_get_client):
        mock_response = MagicMock()
        mock_response.parsed = [
            ImageTranslation(original="도면1", translated="図1"),
            ImageTranslation(original="제어부", translated="制御部"),
        ]
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        mock_get_client.return_value = mock_client

        img = Image.new("RGB", (20, 20), color="white")
        collector = MetricsCollector(NullSink())

        result = translate_image_with_gemini(img, metrics=collector)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].translated, "図1")
        self.assertEqual(collector._counters["n_image_api_calls"], 1)

    @patch("utils.translation._get_client")
    def test_catches_oserror_and_returns_empty(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = OSError(
            "cannot find loader for this WMF file"
        )
        mock_get_client.return_value = mock_client

        img = Image.new("RGB", (20, 20), color="white")
        collector = MetricsCollector(NullSink())

        result = translate_image_with_gemini(img, metrics=collector)
        self.assertEqual(result, [])

    @patch("utils.translation._get_client")
    def test_propagates_quota_exhausted(self, mock_get_client):
        from google.genai.errors import ClientError

        mock_client = MagicMock()
        client_err = ClientError(
            429,
            {
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Resource has been exhausted: free_tier requests per day",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [{"quotaMetric": "free_tier"}],
                        }
                    ],
                }
            },
        )
        mock_client.models.generate_content.side_effect = client_err
        mock_get_client.return_value = mock_client

        img = Image.new("RGB", (20, 20), color="white")
        with self.assertRaises(QuotaExhaustedError):
            translate_image_with_gemini(img)


class TestTranslationRunnerResilience(unittest.TestCase):
    @patch("utils.translation_runner.translate_text_with_gemini")
    @patch("utils.translation_runner.translate_image_with_gemini")
    def test_parallel_translation_with_failing_figure_chunk(
        self, mock_translate_img, mock_translate_text
    ):
        mock_translate_text.side_effect = lambda paragraphs, *args, **kwargs: [
            f"ja-{p}" for p in paragraphs
        ]
        # Simulate image translation failure (raising exception)
        mock_translate_img.side_effect = OSError("cannot find loader for this WMF file")

        chunks = [
            {"type": "TEXT", "content": ["단락 1", "단락 2"]},
            {"type": "FIGURE", "content": Image.new("RGB", (10, 10))},
            {"type": "TEXT", "content": ["단락 3"]},
        ]

        collector = MetricsCollector(NullSink())
        results = translate_chunks_parallel(
            chunks, model_name="test-model", max_workers=2, metrics_collector=collector
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["translated"], ["ja-단락 1", "ja-단락 2"])
        self.assertEqual(results[1]["translated"], [])
        self.assertEqual(results[2]["translated"], ["ja-단락 3"])

    @patch("utils.translation_runner.translate_text_with_gemini")
    @patch("utils.translation_runner.translate_image_with_gemini")
    def test_sequential_translation_with_failing_figure_chunk(
        self, mock_translate_img, mock_translate_text
    ):
        mock_translate_text.side_effect = lambda paragraphs, *args, **kwargs: [
            f"ja-{p}" for p in paragraphs
        ]
        mock_translate_img.side_effect = RuntimeError("Image processing failed")

        chunks = [
            {"type": "TEXT", "content": ["단락 1"]},
            {"type": "FIGURE", "content": Image.new("RGB", (10, 10))},
        ]

        results = translate_chunks_sequential(chunks, model_name="test-model")
        self.assertEqual(results[0]["translated"], ["ja-단락 1"])
        self.assertEqual(results[1]["translated"], [])


class TestDocxParserImageHandling(unittest.TestCase):
    @patch("utils.docx_parser.Document")
    @patch("utils.docx_parser.load_or_convert_image")
    def test_parse_docx_skips_unsupported_images(self, mock_load_img, mock_doc_cls):
        # Mock rels
        mock_rel_valid = MagicMock()
        mock_rel_valid.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        mock_rel_valid.rId = "rId1"
        mock_rel_valid.target_part.blob = b"VALID_PNG"

        mock_rel_broken = MagicMock()
        mock_rel_broken.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        mock_rel_broken.rId = "rId2"
        mock_rel_broken.target_part.blob = b"BROKEN_WMF"

        mock_doc = MagicMock()
        mock_doc.part._rels = {"rId1": mock_rel_valid, "rId2": mock_rel_broken}
        mock_doc.paragraphs = []
        mock_doc_cls.return_value = mock_doc

        # load_or_convert_image returns an image for VALID_PNG, None for BROKEN_WMF
        valid_img = Image.new("RGB", (10, 10))
        mock_load_img.side_effect = lambda b: valid_img if b == b"VALID_PNG" else None

        elements = parse_docx_with_images("dummy.docx")
        self.assertEqual(elements, [])


if __name__ == "__main__":
    unittest.main()
