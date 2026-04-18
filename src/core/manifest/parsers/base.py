from abc import ABC, abstractmethod
from pathlib import Path

from src.core.manifest.models import ManifestInfo


class BaseManifestParser(ABC):
    @abstractmethod
    def can_handle(self, store: dict[str, str]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def parse(self, store: dict[str, str]) -> ManifestInfo:
        raise NotImplementedError

    @staticmethod
    def _find_file(store: dict[str, str], *names: str) -> tuple[str, str] | None:
        for path, content in store.items():
            if Path(path).name in names:
                return path, content
        return None

    @staticmethod
    def _has_file(store: dict[str, str], *names: str) -> bool:
        files = {Path(p).name for p in store.keys()}
        return any(name in files for name in names)
