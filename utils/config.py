# Gemini API-related configurations
DEFAULT_GEMINI_MODEL_NAME = "gemini-2.5-flash"
DEFAULT_GEMINI_MODEL_DISPLAY_NAME = "Gemini 2.5 Flash"

# Parallel translation: max concurrent API requests (Tier 1 friendly).
# Bumped from 8 → 16 after observing ample headroom in metrics
# (peak RAM ~430MB on the largest run, CPU 1-3%, zero 429s on clean runs).
TRANSLATION_MAX_WORKERS = 16

# Discord failure alerts. Webhook URL via st.secrets["discord_webhook_url"] or
# env DISCORD_WEBHOOK_URL (handled in utils/notifications.py). An alert fires
# once the SAME document fails this many times in a row within a session.
DISCORD_ALERT_THRESHOLD = 2

# Metrics → Google Sheets sink. Off by default; flip via env METRICS_ENABLED=1
# or st.secrets["metrics_enabled"] (handled in utils/metrics.py).
METRICS_ENABLED_ENV_VAR = "METRICS_ENABLED"
METRICS_SAMPLE_INTERVAL_S = 1.5
METRICS_FLUSH_INTERVAL_S = 30.0
METRICS_FLUSH_BATCH_SIZE = 30
METRICS_MAX_BUFFER_ROWS = 2000
METRICS_SINK_IO_LOCK_TIMEOUT_S = 3.0
METRICS_THREAD_JOIN_TIMEOUT_S = 1.0
METRICS_ERROR_SHORT_MAX_LEN = 500

# Prompts for translation
TEXT_TRANSLATION_PROMPT = (
    "You are a professional patent translator specializing in Korean-to-Japanese patents. "
    "You will receive a JSON array of Korean patent paragraphs. "
    "Translate each paragraph into Japanese and return a JSON array of the same length. "
    "Each element in the output array must be the translated version of the corresponding input element. "
    "Follow these rules strictly:\n"
    "1. The output array MUST have exactly the same number of elements as the input array.\n"
    "2. Translate each paragraph independently in order; do NOT merge, split, or reorder.\n"
    "3. Preserve headings, numbering, and symbols within each paragraph.\n"
    "4. Do NOT add explanations, clarifications, or additional wording.\n"
    "5. Use formal Japanese patent specification style appropriate for JPO filings.\n"
    "6. Prefer standard Japanese patent terminology.\n"
    "7. Keep technical terms consistent throughout.\n"
    "8. Do NOT omit any content, even if repetitive.\n"
    "9. Empty strings in the input must remain empty strings in the output.\n"
)


IMAGE_TRANSLATION_PROMPT = (
    "You are a patent document processing assistant. "
    "Extract ALL visible Korean or English text from the provided patent drawing image. "
    "Translate each extracted text into Japanese using formal technical terminology. "
    "Follow these rules strictly:\n"
    "1. Do NOT omit any text, including labels, symbols, or reference numerals.\n"
    "2. Do NOT interpret or explain.\n"
    "3. Do NOT reorganize.\n"
    "4. Keep each item independent.\n"
    "Return the result as a JSON array with the exact keys:\n"
    '[{"original": "...", "translated": "..."}]\n'
)
