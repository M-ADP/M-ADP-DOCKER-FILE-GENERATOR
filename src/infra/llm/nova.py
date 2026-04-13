import logging
from typing import Any, List, Optional

from langchain_aws import ChatBedrockConverse

from src.core.llm import LLM

logger = logging.getLogger(__name__)

BEDROCK_MODEL_ID = "apac.amazon.nova-pro-v1:0"


class NovaLLM(LLM):
    def __init__(
        self,
        model: str = BEDROCK_MODEL_ID,
        template: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ):
        super().__init__(
            template=template,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        self.client = ChatBedrockConverse(
            model=model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            region_name="ap-northeast-2",
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
