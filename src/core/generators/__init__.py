from abc import ABC, abstractmethod

from src.core.manifest.models import ManifestInfo


class BaseDockerfileGenerator(ABC):
    @abstractmethod
    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        manifest: ManifestInfo,
    ) -> tuple[str, str, int]:
        raise NotImplementedError
