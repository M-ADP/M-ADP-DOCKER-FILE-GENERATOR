from abc import ABC, abstractmethod


class BaseDockerfileGenerator(ABC):
    @abstractmethod
    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        context: str,
    ) -> tuple[str, str, int]:
        raise NotImplementedError
