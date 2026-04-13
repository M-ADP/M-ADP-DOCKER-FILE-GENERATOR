from typing import Optional

from pydantic_settings import BaseSettings


class NovaSettings(BaseSettings):
    bedrock_model_id: str = "apac.amazon.nova-pro-v1:0"
    bedrock_region: str = "ap-northeast-2"
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    llm_api_key: Optional[str] = None

    model_config = {"env_prefix": "NOVA_", "env_file": ".env", "extra": "ignore"}
