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
from utils.config import TRANSLATION_MAX_WORKERS

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
    progress_placeholder.progress(0, text="📄 문서 파싱 중...")
    elements = parse_docx_with_images(uploaded_file)
    progress_placeholder.progress(0.5, text="📄 청크 구성 중...")
    chunks = group_paragraphs_to_chunks(elements)
    progress_placeholder.progress(1.0, text="✅ 문서 분석 완료")
    st.session_state.parsed_elements = elements
    st.session_state.chunked_elements = chunks


def build_doc_from_translated_chunks(doc, chunks):
    """Write translated chunks (with chunk['translated'] set) into doc in order."""
    paragraph_counter = 0
    for chunk in chunks:
        if chunk["type"] == "TEXT":
            translated = chunk["translated"]
            for line in translated.split("\n"):
                if line.strip():
                    if not (line.startswith("【") and line.endswith("】")):
                        if paragraph_counter == 0:
                            paragraph_counter += 1
                        else:
                            paragraph_number = f" 【{paragraph_counter:04d}】"
                            paragraph_counter += 1
                            doc.add_paragraph_with_justify(" " + paragraph_number)
                    doc.add_paragraph_with_justify(" " + line)
                else:
                    doc.add_paragraph_with_justify("")
        elif chunk["type"] == "FIGURE":
            formatted = [
                f"{p.original}: {p.translated}" for p in chunk["translated"]
            ]
            for line in formatted:
                doc.add_paragraph_with_justify(line)


# 번역 실행
def run_translation():
    chunks = st.session_state.chunked_elements
    total = len(chunks)
    doc = create_japanese_patent_docx()

    def progress_cb(completed, total_n):
        progress_placeholder.progress(
            completed / total_n,
            text=f"🔄 번역 중... {completed} / {total_n} 청크 완료",
        )

    progress_placeholder.progress(0, text=f"🔄 번역 중... 0 / {total} 청크 완료")
    translated_chunks = translate_chunks_parallel(
        chunks,
        model_name=DEFAULT_GEMINI_MODEL_NAME,
        max_workers=TRANSLATION_MAX_WORKERS,
        progress_callback=progress_cb,
    )
    st.session_state.chunked_elements = translated_chunks
    build_doc_from_translated_chunks(doc, translated_chunks)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp_file:
        doc.save(tmp_file.name)
        st.session_state.output_path = tmp_file.name

    st.session_state.translated = True


# 번역 시작 버튼
if uploaded_file and not st.session_state.translated:
    st.button("🚀 번역 시작", on_click=run_translation)

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
        df = pd.DataFrame(st.session_state.chunked_elements)
        st.dataframe(df, width="stretch")
