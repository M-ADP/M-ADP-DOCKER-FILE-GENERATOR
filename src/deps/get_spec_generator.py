from fastapi import Depends

from src.core.llm import LLM
from src.deps.get_nova_llm import get_nova_llm
from src.infra.llm.spec.generator import SpecGenerator


def get_spec_generator(llm: LLM = Depends(get_nova_llm)) -> SpecGenerator:
    return SpecGenerator(llm=llm)
