"""Run translation over chunks: sequential (benchmark only) and parallel (app)."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.config import DEFAULT_GEMINI_MODEL_NAME
from utils.metrics import MetricsCollector, NullMetricsCollector
from utils.translation import translate_image_with_gemini, translate_text_with_gemini

log = logging.getLogger(__name__)


def _translate_single_chunk(
    chunk: dict,
    model_name: str,
    metrics: MetricsCollector | NullMetricsCollector,
) -> dict:
    """Translate one chunk (sets chunk['translated']) and return it.

    TEXT chunks: content is list[str], translated becomes list[str] of same length.
    FIGURE chunks: content is PIL image, translated becomes list[ImageTranslation].
    """
    if chunk["type"] == "TEXT":
        paragraphs: list[str] = chunk["content"]
        translated = translate_text_with_gemini(
            paragraphs, model_name, metrics=metrics
        )
        log.info(
            "TEXT chunk translated: %d paragraphs in -> %d out",
            len(paragraphs),
            len(translated),
        )
        chunk["translated"] = translated
    elif chunk["type"] == "FIGURE":
        chunk["translated"] = translate_image_with_gemini(
            chunk["content"], model_name, metrics=metrics
        )
    return chunk


def translate_chunks_sequential(
    chunks: list[dict],
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    progress_callback=None,
    metrics_collector: MetricsCollector | NullMetricsCollector | None = None,
) -> list[dict]:
    """Translate chunks one by one. Used by benchmark only."""
    metrics = metrics_collector or NullMetricsCollector()
    for i, chunk in enumerate(chunks):
        _translate_single_chunk(chunk, model_name, metrics)
        if progress_callback is not None:
            progress_callback(i + 1, len(chunks))
    return chunks


def translate_chunks_parallel(
    chunks: list[dict],
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    max_workers: int = 8,
    progress_callback=None,
    metrics_collector: MetricsCollector | NullMetricsCollector | None = None,
) -> list[dict]:
    """Translate chunks in parallel; return chunks in original order with 'translated' set.

    Fail-fast: first chunk failure raises. Before re-raising we call
    ``record_failed_chunk()`` exactly once so the run metrics row records
    a single failure. In-flight tasks drain naturally when the
    ``ThreadPoolExecutor`` context exits; their results are discarded
    and not counted as additional failures.
    """
    metrics = metrics_collector or NullMetricsCollector()
    total = len(chunks)
    results: list[dict | None] = [None] * total

    def task(index: int):
        chunk = chunks[index]
        return index, _translate_single_chunk(dict(chunk), model_name, metrics)

    completed = 0
    failure_recorded = False
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(task, i): i for i in range(total)}
        try:
            for future in as_completed(futures):
                try:
                    index, translated_chunk = future.result()
                except Exception:
                    if not failure_recorded:
                        metrics.record_failed_chunk()
                        failure_recorded = True
                    raise
                results[index] = translated_chunk
                completed += 1
                if progress_callback is not None:
                    progress_callback(completed, total)
        except Exception:
            # Best-effort cancel of not-yet-started tasks. Already-running
            # ones will be awaited by the context manager but their
            # results are dropped on the floor.
            for f in futures:
                f.cancel()
            raise

    return [c for c in results if c is not None]
