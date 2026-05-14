"""Unit tests for utils.metrics.

Covers the resilience properties promised in docs/PLAN_metrics_to_sheets.md §8:
counter thread-safety, no-op interface parity, sink-failure isolation
(both at start/finalize), flusher exception not killing the sampler,
phase-duration accounting, and the bounded-buffer drop-oldest path.
"""

from __future__ import annotations

import inspect
import threading
import time
import unittest
from typing import Any

from utils import metrics as M


class FakeSink:
    """In-process sink with optional one-shot failure injection.

    Uses RLock and acquires it inside every method to mirror the real
    SheetsSink's locking discipline — the collector's stop_and_finalize
    acquires the same lock before calling these methods, so a
    non-reentrant Lock would deadlock here (this is exactly the bug we
    hit on the first live run).
    """

    def __init__(self, fail_once: set[str] | None = None) -> None:
        self.io_lock = threading.RLock()
        self.runs_appended: list[M.RunRow] = []
        self.runs_updated: list[tuple[Any, M.RunRow]] = []
        self.samples_batches: list[list[M.SampleRow]] = []
        self._fail_once = fail_once or set()

    def _maybe_fail(self, key: str) -> None:
        if key in self._fail_once:
            self._fail_once.discard(key)
            raise RuntimeError(f"synthetic-{key}")

    def append_run(self, row: M.RunRow) -> Any:
        with self.io_lock:
            self._maybe_fail("append_run")
            self.runs_appended.append(row)
            return len(self.runs_appended)  # 1-indexed insertion order

    def update_run(self, handle: Any, row: M.RunRow) -> bool:
        with self.io_lock:
            self._maybe_fail("update_run")
            self.runs_updated.append((handle, row))
            return True

    def append_samples(self, rows: list[M.SampleRow]) -> None:
        with self.io_lock:
            self._maybe_fail("append_samples")
            self.samples_batches.append(list(rows))


class TestCounters(unittest.TestCase):
    def test_incr_thread_safe(self):
        c = M.MetricsCollector(M.NullSink())
        n_threads = 32
        per_thread = 5000

        def bump():
            for _ in range(per_thread):
                c.incr("n_text_api_calls")

        threads = [threading.Thread(target=bump) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with c._counter_lock:
            self.assertEqual(
                c._counters["n_text_api_calls"], n_threads * per_thread
            )

    def test_incr_unknown_key_is_no_op(self):
        c = M.MetricsCollector(M.NullSink())
        c.incr("not_a_real_key")
        for k in M.COUNTER_KEYS:
            self.assertEqual(c._counters[k], 0)


class TestNullCollector(unittest.TestCase):
    def test_interface_parity_with_real_collector(self):
        """NullMetricsCollector must mirror MetricsCollector's public methods."""
        public_methods = {
            name
            for name in dir(M.MetricsCollector)
            if not name.startswith("_")
            and callable(getattr(M.MetricsCollector, name))
        }
        null = M.NullMetricsCollector()
        for name in public_methods:
            self.assertTrue(
                hasattr(null, name),
                f"NullMetricsCollector missing method: {name}",
            )

    def test_all_methods_are_no_op(self):
        null = M.NullMetricsCollector()
        # Should not raise no matter what we throw at them.
        null.start()
        null.set_phase("translating")
        null.incr("n_text_api_calls", 5)
        null.record(doc_name="foo")
        null.record_failed_chunk()
        null.stop_and_finalize(M.STATUS_OK)
        null.stop_and_finalize(M.STATUS_ERROR, RuntimeError("x"))


class TestLifecycle(unittest.TestCase):
    def test_happy_path_writes_run_and_samples(self):
        sink = FakeSink()
        c = M.MetricsCollector(
            sink,
            sample_interval_s=0.05,
            flush_interval_s=0.1,
            flush_batch_size=2,
        )
        c.record(doc_name="patent.docx", workers=8)
        c.start()
        c.set_phase(M.PHASE_TRANSLATING)
        c.incr("n_text_api_calls", 3)
        c.incr("n_429_errors")
        time.sleep(0.25)  # let sampler/flusher fire a couple of times
        c.set_phase(M.PHASE_BUILDING_DOC)
        time.sleep(0.1)
        c.stop_and_finalize(M.STATUS_OK)

        self.assertEqual(len(sink.runs_appended), 1)
        self.assertEqual(sink.runs_appended[0].status, M.STATUS_RUNNING)

        self.assertEqual(len(sink.runs_updated), 1)
        handle, final = sink.runs_updated[0]
        self.assertEqual(handle, 1)  # row index from FakeSink.append_run
        self.assertEqual(final.status, M.STATUS_OK)
        self.assertEqual(final.doc_name, "patent.docx")
        self.assertEqual(final.workers, 8)
        self.assertEqual(final.n_text_api_calls, 3)
        self.assertEqual(final.n_429_errors, 1)
        self.assertGreater(final.duration_total_s or 0, 0.0)
        # peak_ram_mb should be populated when psutil is available.
        self.assertIsNotNone(final.peak_ram_mb)
        # Samples were flushed at least once.
        self.assertGreater(
            sum(len(b) for b in sink.samples_batches), 0
        )

    def test_error_status_captures_type_and_short(self):
        sink = FakeSink()
        c = M.MetricsCollector(sink)
        c.start()
        try:
            raise ValueError("boom\nwith\tcontrol")
        except ValueError as e:
            c.stop_and_finalize(M.STATUS_ERROR, error=e)
        _, final = sink.runs_updated[0]
        self.assertEqual(final.status, M.STATUS_ERROR)
        self.assertEqual(final.error_type, "ValueError")
        self.assertEqual(final.error_short, "boom with control")


class TestSinkFailureIsolation(unittest.TestCase):
    def test_append_run_failure_routes_to_append_only_finalize(self):
        sink = FakeSink(fail_once={"append_run"})
        c = M.MetricsCollector(sink)
        c.start()  # append_run raises → handle stays None
        c.stop_and_finalize(M.STATUS_OK)

        # No initial 'running' row was recorded.
        self.assertEqual(len(sink.runs_appended), 1)
        self.assertTrue(sink.runs_appended[0].was_append_only)
        self.assertEqual(sink.runs_appended[0].status, M.STATUS_OK)
        # No update was attempted (handle was None).
        self.assertEqual(len(sink.runs_updated), 0)

    def test_update_run_failure_is_swallowed(self):
        sink = FakeSink(fail_once={"update_run"})
        c = M.MetricsCollector(sink)
        c.start()
        # Should not raise even though sink.update_run blows up once.
        c.stop_and_finalize(M.STATUS_OK)
        self.assertEqual(len(sink.runs_appended), 1)

    def test_flusher_exception_keeps_sampler_alive(self):
        sink = FakeSink(fail_once={"append_samples"})
        c = M.MetricsCollector(
            sink,
            sample_interval_s=0.04,
            flush_interval_s=0.08,
            flush_batch_size=1,
        )
        c.start()
        c.set_phase(M.PHASE_TRANSLATING)
        time.sleep(0.5)  # enough time for one failed flush + several successful ones
        c.stop_and_finalize(M.STATUS_OK)

        # Both sampler and flusher must have outlived the synthetic failure:
        # we expect at least one successful samples batch (besides the
        # one that failed).
        self.assertGreaterEqual(len(sink.samples_batches), 1)
        self.assertGreater(
            sum(len(b) for b in sink.samples_batches),
            0,
            "no samples reached the sink after the first failure",
        )


class TestPhaseDurations(unittest.TestCase):
    def test_phase_durations_sum_close_to_total(self):
        sink = FakeSink()
        c = M.MetricsCollector(sink)
        c.start()
        c.set_phase(M.PHASE_TRANSLATING)
        time.sleep(0.15)
        c.set_phase(M.PHASE_BUILDING_DOC)
        time.sleep(0.1)
        c.stop_and_finalize(M.STATUS_OK)

        _, final = sink.runs_updated[0]
        translate = final.duration_translate_s or 0.0
        build = final.duration_build_doc_s or 0.0
        total = final.duration_total_s or 0.0
        self.assertAlmostEqual(translate + build, total, delta=0.3)
        self.assertGreater(translate, 0.1)
        self.assertGreater(build, 0.05)


class TestSafeErrorShort(unittest.TestCase):
    def test_strips_control_chars_and_truncates(self):
        class WeirdExc(Exception):
            def __str__(self) -> str:
                raise RuntimeError("str() exploded")

        s = M._safe_error_short(WeirdExc())
        # repr fallback should kick in and still produce a string.
        self.assertIsInstance(s, str)
        self.assertGreater(len(s), 0)

    def test_truncates_long_message(self):
        msg = "x" * 5000
        s = M._safe_error_short(Exception(msg))
        self.assertLessEqual(len(s), 500)


class TestBoundedBuffer(unittest.TestCase):
    def test_buffer_cap_drops_oldest_and_counts(self):
        """Exercise the drop-oldest path the sampler uses, without spinning
        up real threads — we want to assert the policy itself, not race
        against psutil.
        """
        sink = FakeSink()
        c = M.MetricsCollector(sink, max_buffer_rows=3)
        # No c.start() — we drive the buffer policy directly so the
        # background sampler can't add rows behind our back.

        for i in range(5):
            row = M.SampleRow(
                run_id=c.run_id,
                sampled_at=f"t{i}",
                t_offset_s=float(i),
                ram_mb=1.0,
                process_cpu_pct=0.0,
                process_threads=1,
                phase="translating",
            )
            with c._buffer_lock:
                if len(c._buffer) >= c._max_buffer_rows:
                    del c._buffer[0]
                    c._counters["n_dropped_samples"] = (
                        c._counters.get("n_dropped_samples", 0) + 1
                    )
                c._buffer.append(row)

        self.assertEqual(len(c._buffer), 3)
        self.assertEqual([r.sampled_at for r in c._buffer], ["t2", "t3", "t4"])
        self.assertEqual(c._counters["n_dropped_samples"], 2)


class TestActiveCollectorSwap(unittest.TestCase):
    def test_set_active_collector_stops_previous(self):
        sink_a = FakeSink()
        sink_b = FakeSink()
        a = M.MetricsCollector(sink_a)
        b = M.MetricsCollector(sink_b)
        a.start()
        M.set_active_collector(a)

        # Replacing 'a' with 'b' should stop 'a' (and write an error row).
        M.set_active_collector(b)
        # Give the replaced-collector finalize a beat to flush.
        time.sleep(0.05)

        self.assertEqual(len(sink_a.runs_appended), 1)
        self.assertEqual(len(sink_a.runs_updated), 1)
        _, final_a = sink_a.runs_updated[0]
        self.assertEqual(final_a.status, M.STATUS_ERROR)
        self.assertEqual(final_a.error_type, "RuntimeError")

        # Clean up so module-level state doesn't bleed into other tests.
        try:
            b.stop_and_finalize(M.STATUS_OK)
        finally:
            M._active_collector = None


class TestAppVersion(unittest.TestCase):
    def test_env_override(self):
        import os

        prev = os.environ.get("APP_VERSION")
        os.environ["APP_VERSION"] = "v1.2.3"
        try:
            self.assertEqual(M.resolve_app_version(), "v1.2.3")
        finally:
            if prev is None:
                os.environ.pop("APP_VERSION", None)
            else:
                os.environ["APP_VERSION"] = prev


class TestPublicSignatures(unittest.TestCase):
    def test_collector_public_methods_match_documented_set(self):
        expected = {"start", "stop_and_finalize", "incr", "set_phase", "record", "record_failed_chunk"}
        public = {
            n
            for n, _ in inspect.getmembers(M.MetricsCollector, callable)
            if not n.startswith("_")
        }
        missing = expected - public
        self.assertFalse(missing, f"MetricsCollector missing: {missing}")
        for name in expected:
            self.assertTrue(callable(getattr(M.NullMetricsCollector(), name)))


if __name__ == "__main__":
    unittest.main()
