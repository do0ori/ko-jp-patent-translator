import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from utils import (
    create_japanese_patent_docx,
    group_paragraphs_to_chunks,
    parse_docx_with_images,
    translate_image_with_gemini,
    translate_text_with_gemini,
)
from utils.models import get_model_list, get_default_model

# 초기 상태
if "translated" not in st.session_state:
    st.session_state.translated = False
if "output_path" not in st.session_state:
    st.session_state.output_path = None
if "parsed_elements" not in st.session_state:
    st.session_state.parsed_elements = []
if "chunked_elements" not in st.session_state:
    st.session_state.chunked_elements = []
if "selected_model" not in st.session_state:
    st.session_state.selected_model = get_default_model()

# 페이지 기본 정보
st.set_page_config(page_title="한일 특허 번역기", layout="centered")
st.title("📄 한일 특허 번역기")
st.markdown(
    "업로드한 특허 문서를 자동 분석하여 AI 기반으로 한일 번역 문서를 생성합니다."
)

# 모델 선택 섹션
models = get_model_list()

# 모델 선택 옵션 생성
model_options = []
for model_id, model_info in models.items():
    display_name = model_info["name"]
    if model_info.get("recommended", False):
        display_name += " ⭐ (추천)"
    model_options.append((model_id, display_name))

# 모델 선택 드롭다운
selected_model_id = st.selectbox(
    "🤖 번역에 사용할 AI 모델을 선택하세요",
    options=[opt[0] for opt in model_options],
    index=[opt[0] for opt in model_options].index(st.session_state.selected_model),
    format_func=lambda x: next(opt[1] for opt in model_options if opt[0] == x)
)

# 선택된 모델 정보 표시
if selected_model_id in models:
    model_info = models[selected_model_id]
    st.info(f"**선택된 모델:** {model_info['name']}\n\n**설명:** {model_info['description']}")

# 세션 상태 업데이트
st.session_state.selected_model = selected_model_id

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


# 번역 실행
def run_translation():
    doc = create_japanese_patent_docx()
    total = len(st.session_state.chunked_elements)
    paragraph_counter = 0

    for i, chunk in enumerate(st.session_state.chunked_elements):
        if chunk["type"] == "TEXT":
            translated = translate_text_with_gemini(chunk["content"], st.session_state.selected_model)
            for line in translated.split("\n"):
                if line.strip():
                    # 제목은 단락 번호를 붙이지 않음 -> 제목이 아닌 line에 대해 단락 번호 추가
                    if not (line.startswith("【") and line.endswith("】")):
                        # 첫 번째 단락은 특허 명칭에 대한 단락이므로 번호를 붙이지 않음
                        if paragraph_counter == 0:
                            paragraph_counter += 1
                        else:
                            paragraph_number = f" 【{paragraph_counter:04d}】"
                            paragraph_counter += 1
                            doc.add_paragraph_with_justify(" " + paragraph_number)
                    doc.add_paragraph_with_justify(" " + line)
                else:
                    doc.add_paragraph_with_justify("")
            chunk["translated"] = translated
        elif chunk["type"] == "FIGURE":
            translated_pairs = translate_image_with_gemini(chunk["content"], st.session_state.selected_model)
            formatted = [f"{p.original}: {p.translated}" for p in translated_pairs]
            for line in formatted:
                doc.add_paragraph_with_justify(line)
            chunk["translated"] = formatted

        progress_placeholder.progress(
            (i + 1) / total, text=f"🔄 번역 중... {i + 1} / {total} 청크 완료"
        )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp_file:
        doc.save(tmp_file.name)
        st.session_state.output_path = tmp_file.name

    st.session_state.translated = True


# 번역 시작 버튼
if uploaded_file and not st.session_state.translated:
    st.button("🚀 번역 시작", on_click=run_translation)

# 번역 완료 후 결과
if st.session_state.translated:
    # 사용된 모델 정보 표시
    used_model_info = models.get(st.session_state.selected_model, {})
    st.success(f"✅ 번역이 완료되었습니다! (사용 모델: {used_model_info.get('name', st.session_state.selected_model)})")

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
        st.dataframe(df, use_container_width=True)
