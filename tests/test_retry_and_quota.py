import unittest
from unittest.mock import patch

from google.genai.errors import ClientError

from utils import translation
from utils.translation import (
    ParagraphMismatchError,
    QuotaExhaustedError,
    RetriesExhaustedError,
    _classify_quota_scope,
    extract_retry_delay_seconds,
)

PER_MINUTE = "generativelanguage.googleapis.com/generate_requests_per_minute_per_project"
PER_DAY = "generativelanguage.googleapis.com/generate_requests_per_day_per_project"
FREE_TIER = "generativelanguage.googleapis.com/generate_content_free_tier_requests"


def make_429(
    metric: str, retry_delay=None, *, wrap: bool = True, quota_id: str | None = None
) -> ClientError:
    """Build a ClientError shaped like a real Gemini 429 RESOURCE_EXHAUSTED.

    ``wrap=True`` mimics the sync httpx path (full body with the top-level
    ``error`` key, which is what sets .status); ``wrap=False`` mimics the
    already-unwrapped replay/aiohttp shape. ``quota_id`` lets a test set
    ``quotaId`` distinct from ``quotaMetric`` (they can disagree in the wild).
    """
    details = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [
                {"quotaMetric": metric, "quotaId": quota_id or metric}
            ],
        }
    ]
    if retry_delay is not None:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": retry_delay,
            }
        )
    inner = {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": f"Resource has been exhausted: {metric}",
        "details": details,
    }
    body = {"error": inner} if wrap else inner
    return ClientError(429, body)


class TestRetryDelayExtraction(unittest.TestCase):
    def test_full_body_integer_seconds(self):
        e = make_429(PER_MINUTE, retry_delay="30s")
        self.assertEqual(extract_retry_delay_seconds(e), 30.0)

    def test_fractional_seconds(self):
        e = make_429(PER_MINUTE, retry_delay="17.5s")
        self.assertEqual(extract_retry_delay_seconds(e), 17.5)

    def test_unwrapped_shape_still_parses(self):
        e = make_429(PER_MINUTE, retry_delay="42s", wrap=False)
        self.assertEqual(extract_retry_delay_seconds(e), 42.0)

    def test_dict_duration(self):
        e = make_429(PER_MINUTE, retry_delay={"seconds": "5", "nanos": 500000000})
        self.assertAlmostEqual(extract_retry_delay_seconds(e), 5.5)

    def test_absent_retry_info_returns_none(self):
        e = make_429(PER_DAY)  # no RetryInfo (typical for daily quota)
        self.assertIsNone(extract_retry_delay_seconds(e))


class TestQuotaScopeClassification(unittest.TestCase):
    def test_per_minute(self):
        self.assertEqual(_classify_quota_scope(make_429(PER_MINUTE)), "per_minute")

    def test_per_day(self):
        self.assertEqual(_classify_quota_scope(make_429(PER_DAY)), "per_day")

    def test_free_tier_is_per_day(self):
        self.assertEqual(_classify_quota_scope(make_429(FREE_TIER)), "per_day")

    def test_free_tier_metric_wins_over_minute_quota_id(self):
        # quotaMetric carries the free-tier (credit-wall) signal while quotaId
        # mentions PerMinute — must still fail fast as per_day, and the
        # free_tier signal must not be dropped by capturing only one field.
        e = make_429(FREE_TIER, quota_id="GenerateRequestsPerMinutePerProjectFreeTier")
        self.assertEqual(_classify_quota_scope(e), "per_day")

    def test_unknown(self):
        self.assertEqual(_classify_quota_scope(make_429("some/opaque_metric")), "unknown")


class TestRetryWithDelay(unittest.TestCase):
    def test_per_day_fails_fast_without_sleeping(self):
        calls = {"n": 0}

        def call():
            calls["n"] += 1
            raise make_429(PER_DAY)

        with patch("utils.translation.time.sleep") as sleep:
            with self.assertRaises(QuotaExhaustedError) as ctx:
                translation.retry_with_delay(call, max_retries=5)

        self.assertEqual(calls["n"], 1)  # no retries on a daily wall
        self.assertEqual(ctx.exception.scope, "per_day")
        sleep.assert_not_called()

    def test_per_minute_retries_then_raises_quota_error(self):
        calls = {"n": 0}

        def call():
            calls["n"] += 1
            raise make_429(PER_MINUTE, retry_delay="0s")

        with patch("utils.translation.time.sleep") as sleep:
            with self.assertRaises(QuotaExhaustedError) as ctx:
                translation.retry_with_delay(call, max_retries=3)

        self.assertEqual(calls["n"], 3)
        self.assertEqual(ctx.exception.scope, "per_minute")
        self.assertEqual(sleep.call_count, 2)  # sleeps between attempts, not after last

    def test_mismatch_exhaustion_raises_retries_exhausted(self):
        def call():
            raise ParagraphMismatchError("nope")

        with self.assertRaises(RetriesExhaustedError):
            translation.retry_with_delay(call, max_retries=2)

    def test_success_after_one_mismatch(self):
        calls = {"n": 0}

        def call():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ParagraphMismatchError("retry me")
            return ["ok"]

        result = translation.retry_with_delay(call, max_retries=5)
        self.assertEqual(result, ["ok"])
        self.assertEqual(calls["n"], 2)


class TestNoSplitOnQuota(unittest.TestCase):
    def test_quota_error_propagates_without_splitting(self):
        paragraphs = [f"p{i}" for i in range(6)]

        with patch(
            "utils.translation._translate_text_batch_with_retry",
            side_effect=QuotaExhaustedError("per_day", "credits gone"),
        ) as mock_batch:
            with self.assertRaises(QuotaExhaustedError):
                translation.translate_text_with_gemini(paragraphs, model_name="m")

        # Exactly one call — no recursive half-splitting.
        self.assertEqual(mock_batch.call_count, 1)

    def test_quota_error_does_not_increment_split_fallback(self):
        from utils.metrics import MetricsCollector, NullSink

        collector = MetricsCollector(NullSink())
        with patch(
            "utils.translation._translate_text_batch_with_retry",
            side_effect=QuotaExhaustedError("per_minute", "rpm"),
        ):
            with self.assertRaises(QuotaExhaustedError):
                translation.translate_text_with_gemini(
                    [f"p{i}" for i in range(6)], model_name="m", metrics=collector
                )

        with collector._counter_lock:
            self.assertEqual(collector._counters["n_split_fallbacks"], 0)


if __name__ == "__main__":
    unittest.main()
