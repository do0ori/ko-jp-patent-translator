import json
import logging
import os
import random
import re
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


class QuotaExhaustedError(RuntimeError):
    """A 429 RESOURCE_EXHAUSTED that could not be recovered within the run.

    ``scope`` is one of:
      - ``"per_day"``   — daily quota / credits gone; will NOT recover this run
                          (reset is at midnight Pacific, or needs billing top-up).
      - ``"per_minute"``— per-minute throttle that outlasted our retries.
      - ``"unknown"``   — quota metric could not be parsed.

    Subclass of ``RuntimeError`` so existing ``except RuntimeError`` sites keep
    catching it, but callers can branch on ``isinstance`` to AVOID splitting a
    chunk on a quota wall (splitting just multiplies doomed/paid requests).
    """

    def __init__(self, scope: str = "unknown", detail: str = ""):
        self.scope = scope
        self.detail = detail
        msg = f"Gemini quota exhausted (scope={scope})"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


class RetriesExhaustedError(RuntimeError):
    """Retries exhausted for a NON-quota cause (e.g. paragraph count mismatch).

    Kept distinct from :class:`QuotaExhaustedError` so the split fallback only
    fires for the recoverable mismatch case, never for rate limiting.
    """


# Backoff ceiling for a single retry sleep (seconds).
MAX_BACKOFF_S = 60


def _error_body(exc: ClientError) -> dict:
    """Return the inner error object from a google.genai APIError.

    ``exc.details`` is stored verbatim and may be either the full body
    ``{"error": {...}}`` (sync httpx path) or the already-unwrapped inner
    object ``{"code","status","details":[...]}`` (replay/aiohttp paths), or a
    message-only fallback with neither key. Normalize all of them to the inner
    object (or ``{}``).
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return {}
    inner = details.get("error")
    return inner if isinstance(inner, dict) else details


def _error_detail_items(exc: ClientError) -> list:
    items = _error_body(exc).get("details")
    return items if isinstance(items, list) else []


def extract_retry_delay_seconds(exc: ClientError) -> float | None:
    """Pull the server-suggested retry delay from a google.rpc.RetryInfo detail.

    Returns ``None`` when absent (common for per-day quota 429s) or fractional
    parsing fails, so the caller falls back to its own backoff. Handles both the
    ``"30s"`` / ``"17.5s"`` string Duration and the ``{"seconds","nanos"}`` dict.
    """
    for item in _error_detail_items(exc):
        if not isinstance(item, dict):
            continue
        if str(item.get("@type", "")).endswith("RetryInfo"):
            raw = item.get("retryDelay")
            if isinstance(raw, str):
                m = re.match(r"\s*([0-9]*\.?[0-9]+)s", raw)
                if m:
                    return float(m.group(1))
            elif isinstance(raw, dict):
                return float(raw.get("seconds", 0)) + float(raw.get("nanos", 0)) / 1e9
    return None


def _quota_detail(exc: ClientError) -> str:
    """Human-readable quota info: the error message plus any QuotaFailure ids.

    This is exactly what the previous code SWALLOWED — it carries the decisive
    'free_tier' / 'PerDay' / 'PerMinute' signal that distinguishes a transient
    throttle from a hard daily/credit wall.
    """
    parts: list[str] = []
    msg = getattr(exc, "message", None)
    if msg:
        parts.append(str(msg))
    for item in _error_detail_items(exc):
        if not isinstance(item, dict) or not str(item.get("@type", "")).endswith(
            "QuotaFailure"
        ):
            continue
        for v in item.get("violations", []) or []:
            if isinstance(v, dict):
                # Capture BOTH fields — the decisive 'free_tier'/'PerDay' token
                # can live in either one, so picking just one can drop it.
                for key in ("quotaMetric", "quotaId"):
                    val = v.get(key)
                    if val:
                        parts.append(str(val))
    return " | ".join(parts)


def _classify_quota_scope(exc: ClientError) -> str:
    """'per_day' | 'per_minute' | 'unknown' from the quota metric/message.

    Precedence is deliberate:
      1. 'free_tier' → per_day. On a *paid* project a free-tier quota id only
         appears once credits are exhausted (the project falls back to free
         tier) — a wall that will NOT clear within this run, so fail fast even
         if a 'PerMinute' token co-occurs.
      2. explicit 'day'/'daily' → per_day.
      3. 'minute' → per_minute (transient; worth a backoff retry).
    """
    text = _quota_detail(exc).lower()
    if "free_tier" in text or "freetier" in text:
        return "per_day"
    if "day" in text or "daily" in text:
        return "per_day"
    if "minute" in text:
        return "per_minute"
    return "unknown"


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
    last_quota_error: ClientError | None = None
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
                last_quota_error = e
                metrics.incr("n_429_errors")
                scope = _classify_quota_scope(e)
                # Surface the REAL quota message (free_tier / PerDay / PerMinute)
                # — previously swallowed behind a generic "Retrying" log.
                logging.warning(
                    "RESOURCE_EXHAUSTED [%s]: %s", scope, _quota_detail(e)
                )
                # A daily / credit wall will NOT recover within this run. Fail
                # fast instead of burning retries — and, since splitting is
                # disabled for this error, this stops the request-amplification
                # storm at the source.
                if scope == "per_day":
                    raise QuotaExhaustedError(scope, _quota_detail(e)) from e
                if is_last:
                    continue
                # Exponential backoff with full jitter, honoring the server's
                # RetryInfo hint when present. Jitter desynchronizes the worker
                # threads so they stop retrying in lockstep.
                server_delay = extract_retry_delay_seconds(e)
                if server_delay is not None:
                    sleep_s = server_delay + random.uniform(0, 1.0)
                else:
                    cap = min(default_delay * (2**attempt), MAX_BACKOFF_S)
                    sleep_s = random.uniform(0, cap)
                logging.warning(f"RESOURCE_EXHAUSTED. Retrying in {sleep_s:.1f}s...")
                time.sleep(sleep_s)
                metrics.incr("n_429_retries")
            else:
                logging.error(f"Unexpected ClientError: {e}")
                raise e
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            raise e
    if last_quota_error is not None:
        # Retries exhausted on a (per-minute/unknown) 429 — typed so callers
        # never route a quota failure into the split fallback.
        raise QuotaExhaustedError(
            _classify_quota_scope(last_quota_error),
            _quota_detail(last_quota_error),
        ) from last_quota_error
    raise RetriesExhaustedError(
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
    except QuotaExhaustedError:
        # NEVER split on a quota/credit wall. Splitting recursively re-calls
        # this function on each half, multiplying doomed (and, on pay-as-you-go,
        # billed) requests against an already-empty quota — turning one doomed
        # 99-paragraph call into ~983 of them. Let it propagate immediately.
        raise
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
