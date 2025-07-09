# Pipeline
```markdown
[1] Streamlit:
     ├─ 입력 파일 업로드 (.docx)
    ↓
[2] 문서 순차 파싱 → [(텍스트), (도면), ...] 리스트화
    ↓
[3] 텍스트 chunk 분할 (문장 단위 유지)
    ↓
[4] 출력용 .docx 초기화 (MS Mincho 10.5pt)
    ↓
[5] 요소별 반복 처리
     ├─ 텍스트:
     │   ├─ AI로 전문 용어 추출
     │   ├─ AI로 미포함 용어 검색해서 사용 사례 출처 확보 & 번역 (원문|번역|출처 형식) → 사전 갱신
     │   └─ AI로 번역 (사전 활용)
     ├─ 도면:
     │   └─ AI로 이미지에 있는 text를 [원문 – 번역] 생성
    ↓
[6] 출력 .docx 파일에 결과 누적
    ↓
[7] Streamlit:
     └─ 번역된 .docx 다운로드
```
※ 현재는 사전 사용 안하는 버전 & 비용 이슈로 무료인 Gemini api 사용

## 실행

-   `.streamlit/secrets.toml` 파일 생성
    ```
    GEMINI_API_KEY = "your_gemini_api_key"
    ```
-   앱 실행
    ```
    python -m streamlit run app.py
    ```

## 참고자료
https://ai.google.dev/gemini-api/docs/structured-output?hl=ko&lang=python
