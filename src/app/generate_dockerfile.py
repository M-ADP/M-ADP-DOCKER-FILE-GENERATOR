import logging

from fastapi import Depends

from src.app.base_usecase import BaseUseCase
from src.core.exceptions import DockerfileGenerationError, NoSourceFilesError
from src.core.generators import BaseDockerfileGenerator
from src.core.guards import CompositeSecurityGuard
from src.core.manifest.parser import ManifestParser
from src.core.source.collector import SourceCollector
from src.deps.get_composite_guard import get_composite_guard
from src.deps.get_dockerfile_generator import get_dockerfile_generator
from src.deps.get_manifest_parser import get_manifest_parser
from src.deps.get_source_collector import get_source_collector

logger = logging.getLogger(__name__)


class GenerateDockerfileUseCase(BaseUseCase):
    def __init__(
        self,
        security_guard: CompositeSecurityGuard = Depends(get_composite_guard),
        collector: SourceCollector = Depends(get_source_collector),
        manifest_parser: ManifestParser = Depends(get_manifest_parser),
        generator: BaseDockerfileGenerator = Depends(get_dockerfile_generator),
    ):
        self.security_guard = security_guard
        self.collector = collector
        self.manifest_parser = manifest_parser
        self.generator = generator

    async def __call__(self, tar_bytes: bytes) -> tuple[str, str, int]:
        store = self.collector.extract_store(tar_bytes)
        if not store:
            raise NoSourceFilesError()

        tree = self.collector.build_tree(store)
        manifest = self.manifest_parser.parse(store)

        logger.info(
            f"[GenerateDockerfile] files={len(store)}, "
            f"language={manifest.language}, "
            f"pkg_manager={manifest.pkg_manager}"
        )

        try:
            result, dockerignore, port = await self.generator.generate(store, tree, manifest)
            self.security_guard.validate_dockerfile(result)
            self.security_guard.validate_dockerignore(dockerignore)
        except Exception as e:
            raise DockerfileGenerationError() from e

        return result.strip(), dockerignore.strip(), port
