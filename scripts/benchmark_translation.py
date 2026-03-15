"""
Benchmark: sequential vs parallel translation on the same .docx.
Requires GEMINI_API_KEY in environment (e.g. export GEMINI_API_KEY=... or set in shell).

Usage:
  python scripts/benchmark_translation.py path/to/patent.docx
  python scripts/benchmark_translation.py path/to/patent.docx --sequential-only
  python scripts/benchmark_translation.py path/to/patent.docx --parallel-only
"""

import argparse
import os
import sys
import time

# Project root on path for utils
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.chunker import group_paragraphs_to_chunks
from utils.docx_parser import parse_docx_with_images
from utils.config import TRANSLATION_MAX_WORKERS
from utils.translation_runner import (
    translate_chunks_parallel,
    translate_chunks_sequential,
)


def _parse_and_chunk(docx_path: str):
    with open(docx_path, "rb") as f:
        elements = parse_docx_with_images(f)
    return group_paragraphs_to_chunks(elements)


def main():
    parser = argparse.ArgumentParser(description="Benchmark sequential vs parallel translation")
    parser.add_argument("docx", help="Path to .docx file")
    parser.add_argument("--sequential-only", action="store_true", help="Run only sequential")
    parser.add_argument("--parallel-only", action="store_true", help="Run only parallel")
    args = parser.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        print("Error: Set GEMINI_API_KEY in the environment.", file=sys.stderr)
        sys.exit(1)

    docx_path = args.docx
    if not os.path.isfile(docx_path):
        print(f"Error: File not found: {docx_path}", file=sys.stderr)
        sys.exit(1)

    run_seq = not args.parallel_only
    run_par = not args.sequential_only
    chunk_count = len(_parse_and_chunk(docx_path))
    print(f"Chunks: {chunk_count}")
    print()

    t_seq = None
    t_par = None

    if run_seq:
        chunks_seq = _parse_and_chunk(docx_path)
        print("Running sequential translation...")
        t0 = time.perf_counter()
        translate_chunks_sequential(chunks_seq)
        t_seq = time.perf_counter() - t0
        print(f"  Sequential: {t_seq:.1f}s")

    if run_par:
        chunks_par = _parse_and_chunk(docx_path)
        print("Running parallel translation...")
        t0 = time.perf_counter()
        translate_chunks_parallel(
            chunks_par,
            max_workers=TRANSLATION_MAX_WORKERS,
        )
        t_par = time.perf_counter() - t0
        print(f"  Parallel:   {t_par:.1f}s")

    print()
    if t_seq is not None and t_par is not None:
        speedup = t_seq / t_par
        print(f"Summary: sequential {t_seq:.1f}s, parallel {t_par:.1f}s, speedup {speedup:.2f}x")
    elif t_seq is not None:
        print(f"Summary: sequential {t_seq:.1f}s")
    elif t_par is not None:
        print(f"Summary: parallel {t_par:.1f}s")


if __name__ == "__main__":
    main()
