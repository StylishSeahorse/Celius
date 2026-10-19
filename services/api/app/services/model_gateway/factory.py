from functools import lru_cache

from app.config import get_settings
from app.services.model_gateway.base import ModelGateway
from app.services.model_gateway.openai_compatible import OpenAICompatibleGateway


@lru_cache
def get_model_gateway() -> ModelGateway:
    settings = get_settings()
    # llama.cpp's server is OpenAI-compatible; it gets extra probing of /props and /health.
    return OpenAICompatibleGateway.from_settings(settings)
