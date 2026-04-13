from typing import Optional

from src.core.llm import LLM
from src.infra.llm.nova import NovaLLM

def get_llm(
    template: Optional[str] = None,
) -> LLM:
    return NovaLLM(template=template)
