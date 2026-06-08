import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from utils import (
    DEFAULT_GEMINI_MODEL_DISPLAY_NAME,
    DEFAULT_GEMINI_MODEL_NAME,
    create_japanese_patent_docx,
    group_paragraphs_to_chunks,
    parse_docx_with_images,
    translate_chunks_parallel,
)
from utils.config import (
    DISCORD_ALERT_THRESHOLD,
    METRICS_ENABLED_ENV_VAR,
    TRANSLATION_MAX_WORKERS,
)
from utils.metrics import (
    PHASE_BUILDING_DOC,
    PHASE_TRANSLATING,
    STATUS_ERROR,
    STATUS_OK,
    MetricsCollector,
    NullMetricsCollector,
    NullSink,
    set_active_collector,
)
from utils.metrics_sheets import build_sheets_sink_from_secrets
from utils.notifications import notify_discord_failure
from utils.translation import QuotaExhaustedError

log = logging.getLogger(__name__)

# 초기 상태
if "translated" not in st.session_state:
    st.session_state.translated = False
if "output_path" not in st.session_state:
    st.session_state.output_path = None
if "parsed_elements" not in st.session_state:
    st.session_state.parsed_elements = []
if "chunked_elements" not in st.session_state:
    st.session_state.chunked_elements = []

# 페이지 기본 정보
st.set_page_config(page_title="한일 특허 번역기", page_icon="📄", layout="centered")
st.title("📄 한일 특허 번역기")
st.markdown(
    "업로드한 특허 문서를 자동 분석하여 AI 기반으로 한일 번역 문서를 생성합니다."
)
st.markdown(
    f":material/smart_toy: AI 모델: :blue-badge[{DEFAULT_GEMINI_MODEL_DISPLAY_NAME}]"
)

def _resolve_workers() -> int:
    """Hidden tuning knob via ?workers=N query param; falls back to default."""
    raw = st.query_params.get("workers")
    if raw is None:
        return TRANSLATION_MAX_WORKERS
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return TRANSLATION_MAX_WORKERS
    return max(1, min(n, 32))


workers = _resolve_workers()

# 파일 업로드
uploaded_file = st.file_uploader("📤 번역할 .docx 파일을 업로드하세요", type=["docx"])

# 진행률 표시 위치 확보
progress_placeholder = st.empty()

# 파일 제거 or 변경 시 상태 초기화
if uploaded_file is None:
    st.session_state.translated = False
    st.session_state.output_path = None
    st.session_state.parsed_elements = []
    st.session_state.chunked_elements = []
    st.session_state.base_filename = ""
else:
    new_filename = uploaded_file.name
    if st.session_state.get("last_uploaded_filename") != new_filename:
        st.session_state.translated = False
        st.session_state.output_path = None
        st.session_state.parsed_elements = []
        st.session_state.chunked_elements = []
        st.session_state.last_uploaded_filename = new_filename
        st.session_state.base_filename = Path(new_filename).stem

# 파일 업로드 시 문서 파싱
if uploaded_file and not st.session_state.translated:
    elements = parse_docx_with_images(uploaded_file)
    chunks = group_paragraphs_to_chunks(elements)
    st.session_state.parsed_elements = elements
    st.session_state.chunked_elements = chunks


def build_doc_from_translated_chunks(doc, chunks):
    """Write translated chunks (with chunk['translated'] set) into doc in order."""
    paragraph_counter = 0
    for chunk in chunks:
        if chunk["type"] == "TEXT":
            for para in chunk["translated"]:
                if para.strip():
                    if not (para.startswith("【") and para.endswith("】")):
                        if paragraph_counter == 0:
                            paragraph_counter += 1
                        else:
                            paragraph_number = f" 【{paragraph_counter:04d}】"
                            paragraph_counter += 1
                            doc.add_paragraph_with_justify(" " + paragraph_number)
                    doc.add_paragraph_with_justify(" " + para)
                else:
                    doc.add_paragraph_with_justify("")
        elif chunk["type"] == "FIGURE":
            for p in chunk["translated"]:
                doc.add_paragraph_with_justify(f"{p.original}: {p.translated}")


def _metrics_enabled() -> bool:
    """Env var first, then st.secrets — both falsy by default."""
    env = os.environ.get(METRICS_ENABLED_ENV_VAR, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        return bool(st.secrets.get("metrics_enabled", False))
    except Exception:
        return False


def _build_collector(uploaded_file, chunks, workers):
    """Instantiate a real collector when secrets/flag align; Null otherwise.

    The collector itself is harmless to construct (no IO until start()),
    but we still short-circuit to NullMetricsCollector when disabled so
    the sampler/flusher threads aren't spun up for nothing.
    """
    if not _metrics_enabled():
        return NullMetricsCollector()
    sink = build_sheets_sink_from_secrets()
    if sink is None:
        log.info("[metrics] sheets sink unavailable; using NullSink locally")
        sink = NullSink()
    collector = MetricsCollector(sink)

    text_chunks = [c for c in chunks if c["type"] == "TEXT"]
    figure_chunks = [c for c in chunks if c["type"] == "FIGURE"]
    chunk_sizes = [len(c["content"]) for c in text_chunks]
    total_input_chars = sum(
        len(p) for c in text_chunks for p in c["content"]
    )

    file_size = 0
    doc_name = ""
    if uploaded_file is not None:
        doc_name = getattr(uploaded_file, "name", "") or ""
        size = getattr(uploaded_file, "size", None)
        if isinstance(size, int):
            file_size = size

    collector.record(
        doc_name=doc_name,
        file_size_bytes=file_size,
        n_chunks=len(chunks),
        n_text_chunks=len(text_chunks),
        n_figure_chunks=len(figure_chunks),
        n_paragraphs=sum(chunk_sizes),
        n_images=len(figure_chunks),
        chunk_size_max=max(chunk_sizes) if chunk_sizes else 0,
        chunk_size_avg=(sum(chunk_sizes) / len(chunk_sizes)) if chunk_sizes else 0.0,
        total_input_chars=total_input_chars,
        workers=workers,
        model_name=DEFAULT_GEMINI_MODEL_NAME,
    )
    return collector


def _count_output_chars(translated_chunks):
    total = 0
    for c in translated_chunks:
        if c["type"] == "TEXT":
            total += sum(len(p) for p in c.get("translated", []) or [])
        elif c["type"] == "FIGURE":
            for item in c.get("translated", []) or []:
                total += len(getattr(item, "translated", "") or "")
    return total


def _describe_error(error: BaseException | None) -> str:
    """Short Korean reason string for logs / Discord alerts."""
    if isinstance(error, QuotaExhaustedError):
        if error.scope == "per_day":
            return f"Gemini 일일 쿼터/크레딧 소진 (결제·크레딧 확인 필요) — {error.detail}"
        if error.scope == "per_minute":
            return f"Gemini 분당 요청 한도 초과 (일시적) — {error.detail}"
        return f"Gemini 쿼터 초과 — {error.detail}"
    if error is None:
        return "알 수 없는 오류"
    return f"{type(error).__name__}: {error}"


def _show_error_message(error: BaseException | None) -> None:
    """Render a friendly st.error instead of leaking a raw traceback."""
    if isinstance(error, QuotaExhaustedError) and error.scope == "per_day":
        st.error(
            "❌ Gemini 사용량/크레딧이 소진되어 번역에 실패했습니다.\n\n"
            "결제 크레딧 잔액과, API 키가 속한 프로젝트의 결제 연결을 확인해 주세요. "
            "일일 한도라면 태평양시간(PT) 자정에 초기화됩니다."
        )
    elif isinstance(error, QuotaExhaustedError):
        st.error(
            "❌ Gemini 요청 한도를 초과했습니다. 잠시 후 다시 시도하거나 "
            "동시 작업 수(`?workers=N`)를 줄여 주세요."
        )
    else:
        st.error("❌ 번역 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")


def _handle_failure(doc_name: str, error: BaseException | None, workers: int) -> None:
    """Count consecutive failures of this doc; alert Discord past the threshold."""
    counts = st.session_state.setdefault("failure_counts", {})
    counts[doc_name] = counts.get(doc_name, 0) + 1
    n = counts[doc_name]
    log.warning("[run] translation failed (%s, %d회): %s", doc_name, n, _describe_error(error))
    _show_error_message(error)
    if n >= DISCORD_ALERT_THRESHOLD:
        notify_discord_failure(
            doc_name=doc_name,
            consecutive_failures=n,
            reason=_describe_error(error),
            workers=workers,
            model=DEFAULT_GEMINI_MODEL_NAME,
        )


# 번역 실행
def run_translation(workers: int):
    chunks = st.session_state.chunked_elements
    total = len(chunks)
    doc = create_japanese_patent_docx()
    doc_name = (
        getattr(uploaded_file, "name", "")
        or st.session_state.get("last_uploaded_filename", "")
        or "(이름 없음)"
    )

    def progress_cb(completed, total_n):
        progress_placeholder.progress(
            completed / total_n,
            text=f"🔄 번역 중... {completed} / {total_n} 청크 완료",
        )

    collector = _build_collector(uploaded_file, chunks, workers)
    set_active_collector(collector)
    collector.start(initial_phase=PHASE_TRANSLATING)

    status = STATUS_ERROR
    error: BaseException | None = None
    try:
        progress_placeholder.progress(0, text=f"🔄 번역 중... 0 / {total} 청크 완료")
        translated_chunks = translate_chunks_parallel(
            chunks,
            model_name=DEFAULT_GEMINI_MODEL_NAME,
            max_workers=workers,
            progress_callback=progress_cb,
            metrics_collector=collector,
        )
        st.session_state.chunked_elements = translated_chunks

        collector.set_phase(PHASE_BUILDING_DOC)
        build_doc_from_translated_chunks(doc, translated_chunks)
        collector.record(total_output_chars=_count_output_chars(translated_chunks))

        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp_file:
            doc.save(tmp_file.name)
            st.session_state.output_path = tmp_file.name

        st.session_state.translated = True
        status = STATUS_OK
    except Exception as e:
        # Swallow translation failures here (instead of re-raising into a raw
        # Streamlit traceback) so the user gets a friendly message and we can
        # track/alert on repeated failures. Truly fatal BaseExceptions
        # (KeyboardInterrupt/SystemExit) still propagate.
        error = e
    finally:
        try:
            collector.stop_and_finalize(status, error=error)
        except Exception:
            log.exception("[metrics] stop_and_finalize raised; ignoring")

    if status == STATUS_OK:
        # Clear the consecutive-failure streak for this document.
        st.session_state.setdefault("failure_counts", {}).pop(doc_name, None)
    else:
        _handle_failure(doc_name, error, workers)


# 번역 시작 버튼
if uploaded_file and not st.session_state.translated:
    st.button("🚀 번역 시작", on_click=run_translation, args=(workers,))

# 번역 완료 후 결과
if st.session_state.translated:
    st.success("✅ 번역이 완료되었습니다!")

    download_filename = f"{st.session_state.base_filename}_translated_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"

    with open(st.session_state.output_path, "rb") as f:
        st.download_button(
            label="📥 번역된 .docx 다운로드",
            data=f,
            file_name=download_filename,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    # 번역 결과 표시
    with st.expander("📘 최종 번역 결과 (청크 단위)", expanded=False):
        display_rows = []
        for c in st.session_state.chunked_elements:
            row = {"type": c["type"]}
            if c["type"] == "TEXT":
                row["content"] = "\n".join(c["content"])
                row["translated"] = "\n".join(c.get("translated", []))
            elif c["type"] == "FIGURE":
                row["content"] = "(image)"
                row["translated"] = str(c.get("translated", ""))
            display_rows.append(row)
        st.dataframe(pd.DataFrame(display_rows), width="stretch")
