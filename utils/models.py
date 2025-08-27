# Gemini 모델 목록 관리
# 새로운 모델이 출시될 때 이 파일만 수정

from utils.config import DEFAULT_GEMINI_MODEL_NAME

AVAILABLE_MODELS = {
    "gemini-2.5-pro": {
        "name": "Gemini 2.5 Pro",
        "description": "최고 수준의 성능과 추론 능력을 갖춘 모델. 매우 복잡하고 미묘한 뉘앙스의 번역이나 심층적인 이미지 분석이 필요할 때 적합합니다.",
        "recommended": False
    },
    "gemini-2.5-flash": {
        "name": "Gemini 2.5 Flash",
        "description": "성능, 속도, 비용 효율성(무료 할당량)의 균형이 가장 뛰어난 최신 모델. 일반적인 한일 번역 및 이미지 텍스트 추출(OCR) 작업에 가장 이상적입니다.",
        "recommended": True
    },
    "gemini-2.0-flash": {
        "name": "Gemini 2.0 Flash",
        "description": "안정적인 성능을 제공하는 빠른 응답 속도의 모델. 최신 2.5 Flash 이전 세대로, 번역과 같은 일반적인 작업에 꾸준히 사용되어 왔습니다.",
        "recommended": False
    },
}

def get_model_list():
    """사용 가능한 모델 목록을 반환합니다."""
    return AVAILABLE_MODELS

def get_default_model():
    """기본 모델을 반환합니다."""
    return DEFAULT_GEMINI_MODEL_NAME

def is_model_available(model_name: str) -> bool:
    """모델이 사용 가능한지 확인합니다."""
    return model_name in AVAILABLE_MODELS
