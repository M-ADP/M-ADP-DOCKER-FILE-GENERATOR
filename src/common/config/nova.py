from typing import Optional

from pydantic_settings import BaseSettings

from src.common.const.env import VAULT_ENV_FILE


class NovaSettings(BaseSettings):
    bedrock_model_id: str = "global.amazon.nova-2-lite-v1:0"
    bedrock_region: str = "ap-northeast-2"
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    llm_api_key: Optional[str] = None

    model_config = {
        "env_prefix": "NOVA_",
        "env_file": VAULT_ENV_FILE,
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }
