from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.common.const.env import VAULT_ENV_FILE


class AwsSettings(BaseSettings):
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_default_region: str = "ap-northeast-2"

    model_config = SettingsConfigDict(
        env_file=VAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )
