import logging

from fastapi import Depends

from src.app.base_usecase import BaseUseCase
from src.core.agents.priority_analysis import PriorityAnalysisAgent
from src.core.exceptions import DockerfileGenerationError, NoSourceFilesError
from src.core.generators import BaseDockerfileGenerator
from src.core.source.collector import SourceCollector
from src.deps.get_dockerfile_generator import get_dockerfile_generator
from src.deps.get_priority_agent import get_priority_agent

logger = logging.getLogger(__name__)

MAX_CONTEXT_FILES = 20


class GenerateDockerfileUseCase(BaseUseCase):
    def __init__(
        self,
        collector: SourceCollector = Depends(SourceCollector),
        priority_agent: PriorityAnalysisAgent = Depends(get_priority_agent),
        generator: BaseDockerfileGenerator = Depends(get_dockerfile_generator),
    ):
        self.collector = collector
        self.priority_agent = priority_agent
        self.generator = generator

    async def __call__(self, tar_bytes: bytes) -> str:
        store = self.collector.extract_store(tar_bytes)
        if not store:
            raise NoSourceFilesError()

        tree = self.collector.build_tree(store)
        priority_paths = self.priority_agent.get_priority_paths(store, max_priority=3)

        context_parts: list[str] = []
        for path in priority_paths[:MAX_CONTEXT_FILES]:
            content = store.get(path)
            if content:
                context_parts.append(f"=== {path} ===\n{content}")

        context = ""
        if context_parts:
            context = "[우선순위 높은 파일들]\n" + "\n\n".join(context_parts)

        logger.info(
            f"[GenerateDockerfile] files={len(store)}, priority_files={len(priority_paths)}"
        )

        try:
            result = await self.generator.generate(store, tree, context)
        except Exception as e:
            raise DockerfileGenerationError() from e

        return result.strip()
