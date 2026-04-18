import re

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector


class RubyManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    def can_handle(self, store: dict[str, str]) -> bool:
        return self._has_file(store, "Gemfile")

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        result = self._find_file(store, "Gemfile")
        content = result[1] if result else ""

        return ManifestInfo(
            language="ruby",
            runtime_version=self._extract_ruby_version(store),
            dependencies={},
            dev_dependencies={},
            scripts={},
            pkg_manager="bundler",
            pkg_manager_version=None,
            entry_point=self._find_entry(store),
            detected_port=self._port_detector.detect(store),
            raw_deps=self._extract_gems(content),
        )

    @staticmethod
    def _extract_ruby_version(store: dict[str, str]) -> str | None:
        result = None
        for path, content in store.items():
            from pathlib import Path
            if Path(path).name == ".ruby-version":
                return content.strip()
        for path, content in store.items():
            from pathlib import Path
            if Path(path).name == "Gemfile":
                m = re.search(r"ruby\s+['\"](\d+\.\d+[\.\d]*)['\"]", content)
                if m:
                    return m.group(1)
        return None

    @staticmethod
    def _extract_gems(content: str) -> list[str]:
        return re.findall(r"gem\s+['\"]([^'\"]+)['\"]", content)

    @staticmethod
    def _find_entry(store: dict[str, str]) -> str | None:
        for path in store.keys():
            from pathlib import Path
            if Path(path).name in ("config.ru", "app.rb", "server.rb"):
                return path
        return None
