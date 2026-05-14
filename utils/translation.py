import json
import logging
import os
import threading
import time

from google import genai
from google.genai.errors import ClientError
from pydantic import BaseModel

from utils.config import (
    DEFAULT_GEMINI_MODEL_NAME,
    IMAGE_TRANSLATION_PROMPT,
    TEXT_TRANSLATION_PROMPT,
)
from utils.metrics import MetricsCollector, NullMetricsCollector

# API key resolved once (main thread or first thread that needs it)
_api_key = None
_api_key_lock = threading.Lock()

# One client per thread so worker threads don't share httpx client (avoids "client has been closed")
_tls = threading.local()


def _get_client():
    global _api_key
    with _api_key_lock:
        if _api_key is None:
            try:
                import streamlit as st

                _api_key = st.secrets["GEMINI_API_KEY"]
            except Exception:
                _api_key = os.environ.get("GEMINI_API_KEY")
            if not _api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY not found in st.secrets or GEMINI_API_KEY env"
                )
    if not getattr(_tls, "client", None):
        _tls.client = genai.Client(api_key=_api_key)
    return _tls.client


# 구조화 모델
class ImageTranslation(BaseModel):
    original: str
    translated: str


# 로깅 설정
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


class ParagraphMismatchError(Exception):
    """Raised when the translated paragraph count doesn't match the source."""


def _translate_text_batch_with_retry(
    paragraphs: list[str],
    model_name: str,
    max_retries: int,
    metrics: MetricsCollector | NullMetricsCollector,
) -> list[str]:
    expected_len = len(paragraphs)
    input_json = json.dumps(paragraphs, ensure_ascii=False)

    def call_gemini_api():
        metrics.incr("n_text_api_calls")
        response = _get_client().models.generate_content(
            model=model_name,
            contents=[TEXT_TRANSLATION_PROMPT, input_json],
            config={
                "response_mime_type": "application/json",
                "response_schema": list[str],
            },
        )
        result: list[str] = response.parsed
        if len(result) != expected_len:
            raise ParagraphMismatchError(
                f"Expected {expected_len} paragraphs but got {len(result)}"
            )
        return result

    return retry_with_delay(call_gemini_api, max_retries=max_retries, metrics=metrics)


def retry_with_delay(
    func,
    *args,
    metrics: MetricsCollector | NullMetricsCollector | None = None,
    max_retries=5,
    default_delay=10,
    **kwargs,
):
    metrics = metrics or NullMetricsCollector()
    for attempt in range(max_retries):
        is_last = attempt == max_retries - 1
        try:
            logging.info(
                f"Attempt {attempt + 1}/{max_retries} for function {func.__name__}"
            )
            return func(*args, **kwargs)
        except ParagraphMismatchError as e:
            metrics.incr("n_mismatch_errors")
            logging.warning(
                f"Paragraph count mismatch (attempt {attempt + 1}): {e}. Retrying..."
            )
            if not is_last:
                metrics.incr("n_mismatch_retries")
        except ClientError as e:
            if e.code == 429 and e.status == "RESOURCE_EXHAUSTED":
                metrics.incr("n_429_errors")
                retry_delay = default_delay
                try:
                    retry_info = next(
                        detail
                        for detail in e.details["error"]["details"]
                        if detail["@type"] == "type.googleapis.com/google.rpc.RetryInfo"
                    )
                    retry_delay = int(retry_info["retryDelay"].strip("s"))
                except (StopIteration, KeyError, ValueError):
                    logging.warning(
                        "Failed to extract retryDelay. Using default delay."
                    )
                logging.warning(f"RESOURCE_EXHAUSTED. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                if not is_last:
                    metrics.incr("n_429_retries")
            else:
                logging.error(f"Unexpected ClientError: {e}")
                raise e
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            raise e
    raise RuntimeError(
        f"Failed to execute {func.__name__} after {max_retries} retries."
    )


def translate_text_with_gemini(
    paragraphs: list[str],
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    metrics: MetricsCollector | NullMetricsCollector | None = None,
) -> list[str]:
    """Translate a list of paragraphs, returning a list of the same length.

    If a large chunk keeps failing with paragraph-count mismatch, split it into
    smaller batches and retry recursively so one bad chunk does not stall the
    whole translation run for too long.
    """
    if not paragraphs:
        return []

    metrics = metrics or NullMetricsCollector()
    max_retries = 3 if len(paragraphs) >= 80 else 5
    try:
        return _translate_text_batch_with_retry(
            paragraphs,
            model_name=model_name,
            max_retries=max_retries,
            metrics=metrics,
        )
    except RuntimeError:
        if len(paragraphs) <= 1:
            raise
        metrics.incr("n_split_fallbacks")
        mid = len(paragraphs) // 2
        logging.warning(
            "Falling back to split translation for %d paragraphs (%d + %d).",
            len(paragraphs),
            mid,
            len(paragraphs) - mid,
        )
        # Recursive halves MUST receive the same metrics — otherwise all
        # downstream api_call / 429 / mismatch counts from the split would
        # be silently dropped.
        left = translate_text_with_gemini(
            paragraphs[:mid], model_name=model_name, metrics=metrics
        )
        right = translate_text_with_gemini(
            paragraphs[mid:], model_name=model_name, metrics=metrics
        )
        return left + right


def translate_image_with_gemini(
    pil_image,
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    metrics: MetricsCollector | NullMetricsCollector | None = None,
) -> list[ImageTranslation]:
    metrics = metrics or NullMetricsCollector()

    def call_gemini_api():
        metrics.incr("n_image_api_calls")
        response = _get_client().models.generate_content(
            model=model_name,
            contents=[IMAGE_TRANSLATION_PROMPT, pil_image],
            config={
                "response_mime_type": "application/json",
                "response_schema": list[ImageTranslation],
            },
        )
        return response.parsed

    return retry_with_delay(call_gemini_api, metrics=metrics)
