"""Google Sheets sink for :mod:`utils.metrics`.

Kept in its own module so import failures (gspread / google-auth not
installed) don't break the in-memory ``MetricsCollector``. The factory
``build_sheets_sink_from_secrets`` tries env / ``st.secrets`` in order
and returns ``None`` on any failure — callers should fall back to
:class:`utils.metrics.NullSink`.

All gspread calls are serialized through ``self.io_lock`` so the
flusher's periodic flush, ``stop_and_finalize``'s final flush, and the
``runs`` row update never overlap on the same client.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict
from typing import Any

from utils.metrics import RunRow, SampleRow

log = logging.getLogger(__name__)

RUNS_TAB = "runs"
SAMPLES_TAB = "samples"

# Header order is the canonical column order in the sheet — keep
# synchronized with `runs` / `samples` table in docs/PLAN_metrics_to_sheets.md.
_RUN_COLUMNS = [
    "run_id",
    "started_at",
    "ended_at",
    "duration_total_s",
    "duration_translate_s",
    "duration_build_doc_s",
    "doc_name",
    "file_size_bytes",
    "n_paragraphs",
    "n_images",
    "n_chunks",
    "n_text_chunks",
    "n_figure_chunks",
    "chunk_size_max",
    "chunk_size_avg",
    "total_input_chars",
    "total_output_chars",
    "workers",
    "model_name",
    "sample_interval_s",
    "peak_ram_mb",
    "avg_process_cpu_pct",
    "peak_process_threads",
    "n_text_api_calls",
    "n_image_api_calls",
    "n_429_errors",
    "n_429_retries",
    "n_mismatch_errors",
    "n_mismatch_retries",
    "n_split_fallbacks",
    "n_failed_chunks",
    "status",
    "error_type",
    "error_short",
    "app_version",
    "n_dropped_samples",
    "was_append_only",
]

_SAMPLE_COLUMNS = [
    "run_id",
    "sampled_at",
    "t_offset_s",
    "ram_mb",
    "process_cpu_pct",
    "process_threads",
    "phase",
]


def _row_values(row_dict: dict[str, Any], columns: list[str]) -> list[Any]:
    out: list[Any] = []
    for c in columns:
        v = row_dict.get(c)
        if v is None:
            out.append("")
        elif isinstance(v, bool):
            out.append("TRUE" if v else "FALSE")
        else:
            out.append(v)
    return out


class SheetsSink:
    """gspread-backed sink. Tolerant of partial failure.

    The constructor expects an already-opened ``gspread.Spreadsheet`` so
    auth concerns stay in the factory.
    """

    def __init__(self, spreadsheet) -> None:
        self._ss = spreadsheet
        self.io_lock = threading.Lock()
        self._runs_ws = self._ensure_worksheet(RUNS_TAB, _RUN_COLUMNS)
        self._samples_ws = self._ensure_worksheet(SAMPLES_TAB, _SAMPLE_COLUMNS)

    # -- MetricsSink interface --

    def append_run(self, row: RunRow) -> Any:
        with self.io_lock:
            try:
                values = _row_values(asdict(row), _RUN_COLUMNS)
                resp = self._runs_ws.append_row(
                    values,
                    value_input_option="USER_ENTERED",
                    insert_data_option="INSERT_ROWS",
                )
                return self._parse_appended_row_index(resp)
            except Exception:
                log.exception("[metrics-sheets] append_run failed")
                return None

    def update_run(self, handle: Any, row: RunRow) -> bool:
        with self.io_lock:
            row_idx = handle if isinstance(handle, int) else None
            if row_idx is None:
                row_idx = self._lookup_run_row(row.run_id)
            if row_idx is None:
                log.warning(
                    "[metrics-sheets] update_run: row for run_id=%s not found; "
                    "falling back to append",
                    row.run_id,
                )
                try:
                    values = _row_values(asdict(row), _RUN_COLUMNS)
                    self._runs_ws.append_row(
                        values,
                        value_input_option="USER_ENTERED",
                        insert_data_option="INSERT_ROWS",
                    )
                    return True
                except Exception:
                    log.exception("[metrics-sheets] update_run fallback append failed")
                    return False

            try:
                values = _row_values(asdict(row), _RUN_COLUMNS)
                end_col = _col_letter(len(_RUN_COLUMNS))
                # Range without sheet prefix — gspread resolves against
                # the bound worksheet; including the prefix risks the
                # same double-prefix bug seen with append_row's
                # table_range arg.
                rng = f"A{row_idx}:{end_col}{row_idx}"
                self._runs_ws.update(
                    rng, [values], value_input_option="USER_ENTERED"
                )
                return True
            except Exception:
                log.exception("[metrics-sheets] update_run failed")
                return False

    def append_samples(self, rows: list[SampleRow]) -> None:
        if not rows:
            return
        with self.io_lock:
            try:
                payload = [
                    _row_values(asdict(r), _SAMPLE_COLUMNS) for r in rows
                ]
                self._samples_ws.append_rows(
                    payload,
                    value_input_option="USER_ENTERED",
                    insert_data_option="INSERT_ROWS",
                )
            except Exception:
                log.exception(
                    "[metrics-sheets] append_samples failed (n=%d)", len(rows)
                )

    # -- internals --

    def _ensure_worksheet(self, title: str, columns: list[str]):
        try:
            ws = self._ss.worksheet(title)
        except Exception:
            log.info("[metrics-sheets] creating worksheet %s", title)
            ws = self._ss.add_worksheet(
                title=title, rows=1000, cols=max(len(columns), 8)
            )
            ws.append_row(columns, value_input_option="USER_ENTERED")
            return ws

        # Header sanity: if row 1 is empty, seed it.
        try:
            first_row = ws.row_values(1)
        except Exception:
            first_row = []
        if not first_row:
            try:
                ws.append_row(columns, value_input_option="USER_ENTERED")
            except Exception:
                log.exception(
                    "[metrics-sheets] failed to seed header for %s", title
                )
        return ws

    def _parse_appended_row_index(self, resp) -> int | None:
        """gspread returns the appended range like 'runs!A42:Z42' — pull the row."""
        try:
            updates = resp.get("updates", {}) if isinstance(resp, dict) else {}
            updated_range = updates.get("updatedRange", "")
            if "!" in updated_range:
                _, cells = updated_range.split("!", 1)
            else:
                cells = updated_range
            # cells like A42:Z42 → take trailing number
            tail = cells.split(":")[-1]
            digits = "".join(ch for ch in tail if ch.isdigit())
            return int(digits) if digits else None
        except Exception:
            log.exception("[metrics-sheets] could not parse appended row index")
            return None

    def _lookup_run_row(self, run_id: str) -> int | None:
        try:
            ids_column = self._runs_ws.col_values(1)
            for i, value in enumerate(ids_column, start=1):
                if value == run_id:
                    return i
        except Exception:
            log.exception(
                "[metrics-sheets] _lookup_run_row failed for %s", run_id
            )
        return None


def _col_letter(n: int) -> str:
    """1 → A, 26 → Z, 27 → AA. n is 1-indexed."""
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def build_sheets_sink_from_secrets() -> SheetsSink | None:
    """Try env vars / Streamlit secrets and open the target spreadsheet.

    Returns ``None`` if any required piece is missing or auth fails.
    Callers should fall back to :class:`utils.metrics.NullSink`.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception:
        log.info("[metrics-sheets] gspread/google-auth not installed; disabled")
        return None

    secrets = _load_streamlit_secrets()
    service_account = _read_service_account(secrets)
    sheet_id = _read_sheet_id(secrets)
    if not service_account or not sheet_id:
        log.info(
            "[metrics-sheets] missing service account or sheet id — disabled"
        )
        return None

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(
            service_account, scopes=scopes
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheet_id)
        return SheetsSink(spreadsheet)
    except Exception:
        log.exception("[metrics-sheets] failed to authorize/open spreadsheet")
        return None


def _load_streamlit_secrets() -> Any:
    try:
        import streamlit as st

        return st.secrets
    except Exception:
        return None


def _read_service_account(secrets: Any) -> dict | None:
    if secrets is not None:
        try:
            sa = secrets.get("gcp_service_account")  # type: ignore[union-attr]
            if sa:
                # streamlit secrets returns AttrDict-like; convert defensively
                return dict(sa)
        except Exception:
            pass

    env_path = os.environ.get("GCP_SERVICE_ACCOUNT_JSON_PATH")
    if env_path and os.path.exists(env_path):
        try:
            import json

            with open(env_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("[metrics-sheets] failed to read service account file")
    return None


def _read_sheet_id(secrets: Any) -> str | None:
    if secrets is not None:
        try:
            sid = secrets.get("metrics_sheet_id")  # type: ignore[union-attr]
            if sid:
                return str(sid)
        except Exception:
            pass
    return os.environ.get("METRICS_SHEET_ID") or None
