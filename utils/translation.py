import logging
import time

import streamlit as st
from google import genai
from google.genai.errors import ClientError
from pydantic import BaseModel

# Gemini API 설정
client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])


# 구조화 모델
class TranslationResult(BaseModel):
    translated_text: str


class ImageTranslation(BaseModel):
    original: str
    translated: str


# 로깅 설정
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def retry_with_delay(func, *args, max_retries=5, default_delay=10, **kwargs):
    for attempt in range(max_retries):
        try:
            logging.info(
                f"Attempt {attempt + 1}/{max_retries} for function {func.__name__}"
            )
            return func(*args, **kwargs)
        except ClientError as e:
            if e.code == 429 and e.status == "RESOURCE_EXHAUSTED":
                retry_delay = default_delay
                try:
                    retry_info = next(
                        detail
                        for detail in e.details["error"]["details"]
                        if detail["@type"] == "type.googleapis.com/google.rpc.RetryInfo"
                    )
                    retry_delay = int(retry_info["retryDelay"].strip("s"))
                except (StopIteration, KeyError, ValueError):
                    logging.warning(
                        "Failed to extract retryDelay. Using default delay."
                    )
                logging.warning(f"RESOURCE_EXHAUSTED. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                logging.error(f"Unexpected ClientError: {e}")
                raise e
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            raise e
    raise RuntimeError(
        f"Failed to execute {func.__name__} after {max_retries} retries."
    )


def translate_text_with_gemini(text: str) -> str:
    def call_gemini_api():
        prompt = (
            "Translate the following Korean patent document text into Japanese. "
            "Translate it naturally, but maintain technical and structural fidelity. "
            "Translate domain-specific technical terms with reference to official or trusted Japanese sources, rather than literal translation."
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[prompt, text],
            config={
                "response_mime_type": "application/json",
                "response_schema": TranslationResult,
            },
        )
        return response.parsed.translated_text

    return retry_with_delay(call_gemini_api)


def translate_image_with_gemini(pil_image) -> list[ImageTranslation]:
    def call_gemini_api():
        prompt = (
            "Extract all visible Korean or English text from this image, and translate each into Japanese. "
            "Format the result as a list of objects with 'original' and 'translated' keys."
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[prompt, pil_image],
            config={
                "response_mime_type": "application/json",
                "response_schema": list[ImageTranslation],
            },
        )
        return response.parsed

    return retry_with_delay(call_gemini_api)
