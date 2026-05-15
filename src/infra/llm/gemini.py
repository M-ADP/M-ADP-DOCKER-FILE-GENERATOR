import logging
from typing import Any, List, Optional

from langchain_google_genai import ChatGoogleGenerativeAI

from src.common.config.gemini import GeminiSettings
from src.core.llm import LLM

logger = logging.getLogger(__name__)


class GeminiLLM(LLM):
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

        gemini_settings = GeminiSettings()

        self.client = ChatGoogleGenerativeAI(
            model=model or gemini_settings.gemini_model_id,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            google_api_key=gemini_settings.google_api_key,
        )

    async def _call(self, prompt: str) -> Any:
        try:
            response = await self.client.ainvoke(prompt)
            content = response.content
            if not content:
                logger.error(f"[GeminiLLM] empty response. prompt={prompt[:200]}")
                raise ValueError("LLM returned empty response")
            return content
        except Exception as e:
            logger.error(f"[GeminiLLM] failed. prompt length={len(prompt)}, error={e}")
            raise

    async def _batch(self, prompts: List[str]) -> List[Any]:
        responses = await self.client.abatch(prompts)
        return [r.content for r in responses]