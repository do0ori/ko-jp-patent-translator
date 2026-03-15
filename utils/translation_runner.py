"""Run translation over chunks: sequential (benchmark only) and parallel (app)."""

from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.config import DEFAULT_GEMINI_MODEL_NAME
from utils.translation import translate_image_with_gemini, translate_text_with_gemini


def _translate_single_chunk(chunk: dict, model_name: str) -> dict:
    """Translate one chunk in-place (sets chunk['translated']) and return the same chunk."""
    if chunk["type"] == "TEXT":
        chunk["translated"] = translate_text_with_gemini(
            chunk["content"], model_name
        )
    elif chunk["type"] == "FIGURE":
        translated_pairs = translate_image_with_gemini(
            chunk["content"], model_name
        )
        chunk["translated"] = translated_pairs
    return chunk


def translate_chunks_sequential(
    chunks: list[dict],
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    progress_callback=None,
) -> list[dict]:
    """Translate chunks one by one. Used by benchmark only."""
    for i, chunk in enumerate(chunks):
        _translate_single_chunk(chunk, model_name)
        if progress_callback is not None:
            progress_callback(i + 1, len(chunks))
    return chunks


def translate_chunks_parallel(
    chunks: list[dict],
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    max_workers: int = 8,
    progress_callback=None,
) -> list[dict]:
    """Translate chunks in parallel; return chunks in original order with 'translated' set."""
    total = len(chunks)
    results: list[dict | None] = [None] * total

    def task(index: int):
        chunk = chunks[index]
        return index, _translate_single_chunk(dict(chunk), model_name)

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(task, i): i for i in range(total)}
        for future in as_completed(futures):
            index, translated_chunk = future.result()
            results[index] = translated_chunk
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total)

    return [c for c in results if c is not None]
