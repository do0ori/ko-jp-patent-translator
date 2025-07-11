# Gemini API-related configurations
GEMINI_MODEL_NAME = "gemini-2.0-flash"

# Prompts for translation
TEXT_TRANSLATION_PROMPT = (
    "You are a professional translator specializing in Korean-to-Japanese patents. "
    "Translate the following Korean patent text into natural-sounding Japanese, "
    "ensuring that technical terms are translated accurately and the original document's "
    "structure is preserved. Use official or industry-standard Japanese "
    "terminology where applicable."
)

IMAGE_TRANSLATION_PROMPT = (
    "You are a specialized AI assistant for patent document processing. "
    "Extract all visible Korean or English text from the provided image of a patent drawing. "
    "For each extracted text, provide its corresponding Japanese translation. "
    "Return the result as a structured list of pairs, with 'original' and 'translated' keys."
)
