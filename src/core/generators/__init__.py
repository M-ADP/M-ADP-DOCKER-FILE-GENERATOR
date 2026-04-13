from abc import ABC, abstractmethod
from typing import Any


class BaseDockerfileGenerator(ABC):
    @abstractmethod
    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        context: str,
    ) -> str:
        raise NotImplementedError
