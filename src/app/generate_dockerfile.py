import logging
from typing import List

from fastapi import Depends
from langchain_core.runnables import Runnable

from src.app.base_usecase import BaseUseCase
from src.core.exceptions import DockerfileGenerationError, NoSourceFilesError
from src.core.source.collector import SourceCollector
from src.core.source.model import SourceFile
from src.deps.get_chain import get_chain

logger = logging.getLogger(__name__)


def _format_source_code(files: List[SourceFile]) -> str:
    parts = [f"=== {f.path} ===\n{f.content}" for f in files]
    return "\n\n".join(parts)


class GenerateDockerfileUseCase(BaseUseCase):

    def __init__(
        self,
        chain: Runnable = Depends(get_chain),
        collector: SourceCollector = Depends(SourceCollector),
    ):
        self.chain = chain
        self.collector = collector

    async def __call__(self, tar_bytes: bytes) -> str:
        files = self.collector.collect(tar_bytes)  # InvalidArchiveError 그대로 전파
        if not files:
            raise NoSourceFilesError()

        source_code = _format_source_code(files)
        logger.info(f"[GenerateDockerfile] files={len(files)}, chars={len(source_code)}")

        try:
            result = await self.chain.ainvoke({"source_code": source_code})
        except Exception as e:
            raise DockerfileGenerationError() from e

        return result.strip()
