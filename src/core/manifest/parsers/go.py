import re

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector


class GoManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    def can_handle(self, store: dict[str, str]) -> bool:
        return self._has_file(store, "go.mod")

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        result = self._find_file(store, "go.mod")
        content = result[1] if result else ""

        return ManifestInfo(
            language="go",
            runtime_version=self._extract_go_version(content),
            dependencies={},
            dev_dependencies={},
            scripts={},
            pkg_manager="go",
            pkg_manager_version=None,
            entry_point=self._find_main(store),
            detected_port=self._port_detector.detect(store),
            raw_deps=self._extract_deps(content),
        )

    @staticmethod
    def _extract_go_version(content: str) -> str | None:
        m = re.search(r"^go\s+(\d+\.\d+)", content, re.MULTILINE)
        return m.group(1) if m else None

    @staticmethod
    def _extract_deps(content: str) -> list[str]:
        return re.findall(r"^\s+(\S+)\s+v[\d.]+", content, re.MULTILINE)

    @staticmethod
    def _find_main(store: dict[str, str]) -> str | None:
        for path in store.keys():
            from pathlib import Path
            if Path(path).name == "main.go":
                return path
        return None
