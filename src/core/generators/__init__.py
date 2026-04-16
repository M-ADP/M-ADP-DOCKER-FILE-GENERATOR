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

    @abstractmethod
    async def generate_with_feedback(
        self,
        store: dict[str, str],
        tree: str,
        context: str,
        dockerfile: str,
        dockerignore: str,
        feedback: str,
    ) -> tuple[str, str, int]:
        raise NotImplementedError
