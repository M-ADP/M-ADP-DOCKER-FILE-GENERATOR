from typing import Optional

from pydantic_settings import BaseSettings

from src.common.const.env import VAULT_ENV_FILE


class GeminiSettings(BaseSettings):
    gemini_model_id: str = "gemini-2.5-flash"
    gemini_region: str = "ap-northeast-2"
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    google_api_key: Optional[str] = None

    model_config = {
        "env_prefix": "GEMINI_",
        "env_file": VAULT_ENV_FILE,
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }