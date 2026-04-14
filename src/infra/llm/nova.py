import logging
from typing import Any, List, Optional

from langchain_aws import ChatBedrockConverse

from src.common.config.aws import AwsSettings
from src.common.config.nova import NovaSettings
from src.core.llm import LLM

logger = logging.getLogger(__name__)


class NovaLLM(LLM):
    def __init__(
        self,
        model: Optional[str] = None,
        template: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        super().__init__(
            template=template,
            temperature=temperature or 0.7,
            max_tokens=max_tokens,
        )

        aws_settings = AwsSettings()
        nova_settings = NovaSettings()

        self.client = ChatBedrockConverse(
            model=model or nova_settings.bedrock_model_id,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            region_name=aws_settings.aws_default_region,
            aws_access_key_id=aws_settings.aws_access_key_id or None,
            aws_secret_access_key=aws_settings.aws_secret_access_key or None,
        )

    async def _call(self, prompt: str) -> Any:
        try:
            response = await self.client.ainvoke(prompt)
            content = response.content
            if not content:
                logger.error(f"[NovaLLM] empty response. prompt={prompt[:200]}")
                raise ValueError("LLM returned empty response")
            return content
        except Exception as e:
            logger.error(f"[NovaLLM] failed. prompt length={len(prompt)}, error={e}")
            raise

    async def _batch(self, prompts: List[str]) -> List[Any]:
        responses = await self.client.abatch(prompts)
        return [r.content for r in responses]
