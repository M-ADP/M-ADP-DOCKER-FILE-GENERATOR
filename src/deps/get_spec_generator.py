from fastapi import Depends

from src.deps.get_nova_llm import get_nova_llm
from src.infra.llm.nova import NovaLLM
from src.infra.llm.spec.generator import SpecGenerator


def get_spec_generator(llm: NovaLLM = Depends(get_nova_llm)) -> SpecGenerator:
    return SpecGenerator(llm=llm)
