"""Run-level metrics collector for Streamlit Cloud post-mortem analysis.

See ``docs/PLAN_metrics_to_sheets.md`` for the design. Highlights:

- ``MetricsCollector`` runs two background daemons (sampler + flusher) and
  keeps an in-memory bounded sample buffer + counter dict.
- ``NullMetricsCollector`` mirrors the same interface as no-ops so call
  sites never need ``if metrics is not None`` guards.
- Sinks (``MetricsSink``) abstract the storage target — ``NullSink`` /
  ``StdoutSink`` ship here; ``SheetsSink`` is added in a follow-up commit.
- Lock acquisition order is fixed:
  ``_counter_lock`` → ``_buffer_lock`` → sink-internal ``_sink_io_lock``.
- The sampler never touches IO; the flusher swaps the buffer under the
  buffer lock and pushes to the sink with the lock released.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from utils.config import (
    METRICS_ERROR_SHORT_MAX_LEN,
    METRICS_FLUSH_BATCH_SIZE,
    METRICS_FLUSH_INTERVAL_S,
    METRICS_MAX_BUFFER_ROWS,
    METRICS_SAMPLE_INTERVAL_S,
    METRICS_SINK_IO_LOCK_TIMEOUT_S,
    METRICS_THREAD_JOIN_TIMEOUT_S,
)

log = logging.getLogger(__name__)


# ---------- counter / phase keys ----------

COUNTER_KEYS = (
    "n_text_api_calls",
    "n_image_api_calls",
    "n_429_errors",
    "n_429_retries",
    "n_mismatch_errors",
    "n_mismatch_retries",
    "n_split_fallbacks",
    "n_failed_chunks",
    "n_dropped_samples",
)

PHASE_TRANSLATING = "translating"
PHASE_BUILDING_DOC = "building_doc"

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"


# ---------- sink protocol ----------


@dataclass(frozen=True)
class SampleRow:
    run_id: str
    sampled_at: str
    t_offset_s: float
    ram_mb: float
    process_cpu_pct: float
    process_threads: int
    phase: str


@dataclass
class RunRow:
    run_id: str
    started_at: str
    ended_at: str = ""
    duration_total_s: float | None = None
    duration_translate_s: float | None = None
    duration_build_doc_s: float | None = None
    doc_name: str = ""
    file_size_bytes: int = 0
    n_paragraphs: int = 0
    n_images: int = 0
    n_chunks: int = 0
    n_text_chunks: int = 0
    n_figure_chunks: int = 0
    chunk_size_max: int = 0
    chunk_size_avg: float = 0.0
    total_input_chars: int = 0
    total_output_chars: int | None = None
    workers: int = 0
    model_name: str = ""
    sample_interval_s: float = METRICS_SAMPLE_INTERVAL_S
    peak_ram_mb: float | None = None
    avg_process_cpu_pct: float | None = None
    peak_process_threads: int | None = None
    n_text_api_calls: int = 0
    n_image_api_calls: int = 0
    n_429_errors: int = 0
    n_429_retries: int = 0
    n_mismatch_errors: int = 0
    n_mismatch_retries: int = 0
    n_split_fallbacks: int = 0
    n_failed_chunks: int = 0
    status: str = STATUS_RUNNING
    error_type: str = ""
    error_short: str = ""
    app_version: str = ""
    n_dropped_samples: int = 0
    was_append_only: bool = False


class MetricsSink(Protocol):
    """Where run/sample rows get written.

    Implementations MUST be tolerant of partial failure — they swallow
    exceptions and log them so the translation pipeline never sees them.
    """

    def append_run(self, row: RunRow) -> Any:  # returns row handle (e.g. sheet row index) or None
        ...

    def update_run(self, handle: Any, row: RunRow) -> bool:
        ...

    def append_samples(self, rows: list[SampleRow]) -> None: ...


class NullSink:
    def append_run(self, row: RunRow) -> Any:
        return None

    def update_run(self, handle: Any, row: RunRow) -> bool:
        return True

    def append_samples(self, rows: list[SampleRow]) -> None:
        pass


class StdoutSink:
    """For local debugging — pretty-prints rows so you can sanity-check fields."""

    def append_run(self, row: RunRow) -> Any:
        log.info("[metrics] APPEND_RUN %s", row)
        return row.run_id

    def update_run(self, handle: Any, row: RunRow) -> bool:
        log.info("[metrics] UPDATE_RUN handle=%s %s", handle, row)
        return True

    def append_samples(self, rows: list[SampleRow]) -> None:
        for r in rows:
            log.info("[metrics] SAMPLE %s", r)


# ---------- helpers ----------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_error_short(exc: BaseException) -> str:
    try:
        s = str(exc)
    except Exception:
        try:
            s = repr(exc)
        except Exception:
            s = f"<unprintable {type(exc).__name__}>"
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return s[:METRICS_ERROR_SHORT_MAX_LEN]


def resolve_app_version() -> str:
    env = os.environ.get("APP_VERSION")
    if env:
        return env
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        sha = result.stdout.strip()
        if result.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return "unknown"


# ---------- collector ----------


@dataclass
class _PhaseTracker:
    """Tracks elapsed time per phase. set_phase() closes out the previous one."""

    current_phase: str = ""
    phase_started_at: float = 0.0
    durations: dict[str, float] = field(default_factory=dict)

    def transition(self, new_phase: str, now: float) -> None:
        if self.current_phase:
            self.durations[self.current_phase] = (
                self.durations.get(self.current_phase, 0.0)
                + (now - self.phase_started_at)
            )
        self.current_phase = new_phase
        self.phase_started_at = now

    def close(self, now: float) -> None:
        if self.current_phase:
            self.durations[self.current_phase] = (
                self.durations.get(self.current_phase, 0.0)
                + (now - self.phase_started_at)
            )
            self.current_phase = ""


class MetricsCollector:
    """Live, thread-safe metrics aggregator for one translation run.

    Spawns two daemon threads on ``start()``:
      - sampler: reads psutil ~sample_interval_s, appends to buffer
      - flusher: every flush_interval_s (or when buffer >= batch_size)
        swaps the buffer and pushes to the sink

    ``incr`` / ``set_phase`` / ``record`` / ``record_failed_chunk`` are
    safe to call from worker threads. The sampler/flusher never block on
    sink IO under either ``_counter_lock`` or ``_buffer_lock``.
    """

    def __init__(
        self,
        sink: MetricsSink,
        *,
        run_id: str | None = None,
        sample_interval_s: float = METRICS_SAMPLE_INTERVAL_S,
        flush_interval_s: float = METRICS_FLUSH_INTERVAL_S,
        flush_batch_size: int = METRICS_FLUSH_BATCH_SIZE,
        max_buffer_rows: int = METRICS_MAX_BUFFER_ROWS,
    ) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        self._sink = sink
        self._sample_interval_s = sample_interval_s
        self._flush_interval_s = flush_interval_s
        self._flush_batch_size = flush_batch_size
        self._max_buffer_rows = max_buffer_rows

        self._counter_lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._counters: dict[str, int] = {k: 0 for k in COUNTER_KEYS}
        self._buffer: list[SampleRow] = []
        self._phase = _PhaseTracker()

        self._started_at_wall: str = ""
        self._started_at_mono: float = 0.0
        self._run_handle: Any = None
        self._was_append_only = False
        self._run_meta: dict[str, Any] = {}

        self._stop_evt = threading.Event()
        self._flush_kick = threading.Event()
        self._sampler: threading.Thread | None = None
        self._flusher: threading.Thread | None = None
        self._proc = None  # lazy psutil.Process — None means sampling disabled
        self._sample_peaks: dict[str, float | int | None] = {
            "peak_ram_mb": None,
            "peak_threads": None,
            "cpu_sum": 0.0,
            "cpu_count": 0,
        }
        self._peaks_lock = threading.Lock()

    # ----- public API -----

    def start(self) -> None:
        self._started_at_wall = _now_iso()
        self._started_at_mono = time.monotonic()
        self._phase.transition("", self._started_at_mono)  # no-op until set_phase

        run_row = self._snapshot_run_row(STATUS_RUNNING)
        try:
            self._run_handle = self._sink.append_run(run_row)
        except Exception:
            log.exception("[metrics] append_run failed; will use append-only finalize")
            self._run_handle = None
            self._was_append_only = True

        self._proc = self._init_psutil()
        self._sampler = threading.Thread(
            target=self._sampler_loop, name="metrics-sampler", daemon=True
        )
        self._flusher = threading.Thread(
            target=self._flusher_loop, name="metrics-flusher", daemon=True
        )
        self._sampler.start()
        self._flusher.start()

    def stop_and_finalize(
        self,
        status: str,
        error: BaseException | None = None,
    ) -> None:
        now = time.monotonic()
        with self._buffer_lock:
            self._phase.close(now)

        self._stop_evt.set()
        self._flush_kick.set()
        for t in (self._sampler, self._flusher):
            if t is not None:
                t.join(timeout=METRICS_THREAD_JOIN_TIMEOUT_S)

        # Try to acquire sink IO lock with a hard cap so a hung flusher
        # doesn't block the user's page transition forever.
        sink_lock = getattr(self._sink, "io_lock", None)
        acquired = False
        if sink_lock is not None:
            acquired = sink_lock.acquire(timeout=METRICS_SINK_IO_LOCK_TIMEOUT_S)
            if not acquired:
                log.warning(
                    "[metrics] sink_io_lock acquire timed out (%.1fs) — "
                    "skipping final flush and run update; row will remain "
                    "in 'running' status",
                    METRICS_SINK_IO_LOCK_TIMEOUT_S,
                )
                return

        try:
            # final flush
            try:
                final_batch = self._swap_buffer()
                if final_batch:
                    self._sink.append_samples(final_batch)
            except Exception:
                log.exception("[metrics] final samples flush failed")

            row = self._snapshot_run_row(status, error=error)
            try:
                if self._run_handle is None:
                    # append-only fallback — start() couldn't seat the row
                    self._sink.append_run(row)
                else:
                    self._sink.update_run(self._run_handle, row)
            except Exception:
                log.exception("[metrics] run finalize write failed")
        finally:
            if acquired and sink_lock is not None:
                sink_lock.release()

    def incr(self, key: str, n: int = 1) -> None:
        if key not in COUNTER_KEYS:
            log.warning("[metrics] unknown counter key: %s", key)
            return
        with self._counter_lock:
            self._counters[key] += n

    def set_phase(self, phase: str) -> None:
        now = time.monotonic()
        with self._buffer_lock:
            self._phase.transition(phase, now)

    def record(self, **fields: Any) -> None:
        # Fields that flow straight onto the RunRow at finalize time.
        with self._buffer_lock:
            self._run_meta.update(fields)

    def record_failed_chunk(self, n: int = 1) -> None:
        self.incr("n_failed_chunks", n=n)

    # ----- internals -----

    def _init_psutil(self):
        try:
            import psutil  # noqa: F401 — local import so import failures don't kill the app

            proc = psutil.Process()
            proc.cpu_percent(interval=None)  # warm-up; first reading is 0/garbage
            return proc
        except Exception:
            log.exception("[metrics] psutil unavailable — sampling disabled")
            return None

    def _sampler_loop(self) -> None:
        if self._proc is None:
            return
        while not self._stop_evt.is_set():
            try:
                self._take_sample()
            except Exception:
                log.exception("[metrics] sampler iteration failed")
            self._stop_evt.wait(self._sample_interval_s)

    def _take_sample(self) -> None:
        assert self._proc is not None
        now_mono = time.monotonic()
        mem = self._proc.memory_info().rss / (1024 * 1024)
        cpu = self._proc.cpu_percent(interval=None)
        threads = self._proc.num_threads()
        sampled_at = _now_iso()
        t_offset = max(0.0, now_mono - self._started_at_mono)

        with self._buffer_lock:
            phase = self._phase.current_phase or ""
            row = SampleRow(
                run_id=self.run_id,
                sampled_at=sampled_at,
                t_offset_s=round(t_offset, 3),
                ram_mb=round(mem, 2),
                process_cpu_pct=round(cpu, 2),
                process_threads=threads,
                phase=phase,
            )
            if len(self._buffer) >= self._max_buffer_rows:
                # drop oldest, keep newest — analysis cares about the
                # OOM-adjacent tail more than the start.
                del self._buffer[0]
                # safe nested lock: counter_lock is order #1, we already
                # hold buffer_lock (#2) → acquiring #1 here would violate
                # the rule. So we increment directly under the buffer
                # lock for n_dropped_samples — it's a write-only counter
                # so contention with readers is irrelevant.
                self._counters["n_dropped_samples"] = (
                    self._counters.get("n_dropped_samples", 0) + 1
                )
            self._buffer.append(row)
            buf_len = len(self._buffer)

        with self._peaks_lock:
            if (
                self._sample_peaks["peak_ram_mb"] is None
                or mem > self._sample_peaks["peak_ram_mb"]
            ):
                self._sample_peaks["peak_ram_mb"] = mem
            if (
                self._sample_peaks["peak_threads"] is None
                or threads > self._sample_peaks["peak_threads"]
            ):
                self._sample_peaks["peak_threads"] = threads
            self._sample_peaks["cpu_sum"] += cpu
            self._sample_peaks["cpu_count"] += 1

        if buf_len >= self._flush_batch_size:
            self._flush_kick.set()

    def _flusher_loop(self) -> None:
        while not self._stop_evt.is_set():
            triggered = self._flush_kick.wait(timeout=self._flush_interval_s)
            self._flush_kick.clear()
            if self._stop_evt.is_set():
                return
            try:
                batch = self._swap_buffer()
                if batch:
                    self._sink.append_samples(batch)
            except Exception:
                log.exception(
                    "[metrics] flusher iteration failed (triggered=%s)", triggered
                )

    def _swap_buffer(self) -> list[SampleRow]:
        with self._buffer_lock:
            if not self._buffer:
                return []
            batch = self._buffer
            self._buffer = []
            return batch

    def _snapshot_run_row(
        self, status: str, error: BaseException | None = None
    ) -> RunRow:
        with self._counter_lock:
            counters = dict(self._counters)

        with self._buffer_lock:
            meta = dict(self._run_meta)
            phase_durations = dict(self._phase.durations)
            # If we are still in a phase mid-run, account for the live one too
            # so snapshots taken for `running` rows aren't wildly off.
            if self._phase.current_phase:
                live = time.monotonic() - self._phase.phase_started_at
                phase_durations[self._phase.current_phase] = (
                    phase_durations.get(self._phase.current_phase, 0.0) + live
                )

        with self._peaks_lock:
            peaks = dict(self._sample_peaks)

        cpu_avg = None
        if peaks["cpu_count"]:
            cpu_avg = peaks["cpu_sum"] / peaks["cpu_count"]

        ended_at = ""
        duration_total = None
        if status != STATUS_RUNNING:
            ended_at = _now_iso()
            duration_total = round(time.monotonic() - self._started_at_mono, 3)

        return RunRow(
            run_id=self.run_id,
            started_at=self._started_at_wall,
            ended_at=ended_at,
            duration_total_s=duration_total,
            duration_translate_s=round(
                phase_durations.get(PHASE_TRANSLATING, 0.0), 3
            )
            if phase_durations.get(PHASE_TRANSLATING)
            else None,
            duration_build_doc_s=round(
                phase_durations.get(PHASE_BUILDING_DOC, 0.0), 3
            )
            if phase_durations.get(PHASE_BUILDING_DOC)
            else None,
            doc_name=meta.get("doc_name", ""),
            file_size_bytes=int(meta.get("file_size_bytes", 0)),
            n_paragraphs=int(meta.get("n_paragraphs", 0)),
            n_images=int(meta.get("n_images", 0)),
            n_chunks=int(meta.get("n_chunks", 0)),
            n_text_chunks=int(meta.get("n_text_chunks", 0)),
            n_figure_chunks=int(meta.get("n_figure_chunks", 0)),
            chunk_size_max=int(meta.get("chunk_size_max", 0)),
            chunk_size_avg=float(meta.get("chunk_size_avg", 0.0)),
            total_input_chars=int(meta.get("total_input_chars", 0)),
            total_output_chars=meta.get("total_output_chars"),
            workers=int(meta.get("workers", 0)),
            model_name=str(meta.get("model_name", "")),
            sample_interval_s=self._sample_interval_s,
            peak_ram_mb=round(peaks["peak_ram_mb"], 2)
            if peaks["peak_ram_mb"] is not None
            else None,
            avg_process_cpu_pct=round(cpu_avg, 2) if cpu_avg is not None else None,
            peak_process_threads=peaks["peak_threads"],
            n_text_api_calls=counters["n_text_api_calls"],
            n_image_api_calls=counters["n_image_api_calls"],
            n_429_errors=counters["n_429_errors"],
            n_429_retries=counters["n_429_retries"],
            n_mismatch_errors=counters["n_mismatch_errors"],
            n_mismatch_retries=counters["n_mismatch_retries"],
            n_split_fallbacks=counters["n_split_fallbacks"],
            n_failed_chunks=counters["n_failed_chunks"],
            status=status,
            error_type=type(error).__name__ if error is not None else "",
            error_short=_safe_error_short(error) if error is not None else "",
            app_version=str(meta.get("app_version") or resolve_app_version()),
            n_dropped_samples=counters["n_dropped_samples"],
            was_append_only=self._was_append_only,
        )


class NullMetricsCollector:
    """No-op collector matching :class:`MetricsCollector` interface.

    Wired as the default everywhere so call sites stay free of
    ``if metrics is not None`` checks.
    """

    run_id = ""

    def start(self) -> None: ...

    def stop_and_finalize(
        self, status: str, error: BaseException | None = None
    ) -> None: ...

    def incr(self, key: str, n: int = 1) -> None: ...

    def set_phase(self, phase: str) -> None: ...

    def record(self, **fields: Any) -> None: ...

    def record_failed_chunk(self, n: int = 1) -> None: ...


# ---------- module-level active collector (for Streamlit rerun handling) ----------

_active_lock = threading.Lock()
_active_collector: MetricsCollector | NullMetricsCollector | None = None


def set_active_collector(c: MetricsCollector | NullMetricsCollector) -> None:
    global _active_collector
    with _active_lock:
        prev = _active_collector
        _active_collector = c
    if prev is not None and prev is not c and isinstance(prev, MetricsCollector):
        try:
            prev.stop_and_finalize(STATUS_ERROR, RuntimeError("replaced by new run"))
        except Exception:
            log.exception("[metrics] failed to stop previous active collector")


def get_active_collector() -> MetricsCollector | NullMetricsCollector:
    with _active_lock:
        return _active_collector or NullMetricsCollector()
