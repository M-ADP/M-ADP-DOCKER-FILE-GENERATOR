import json

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector


class PhpManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    def can_handle(self, store: dict[str, str]) -> bool:
        return self._has_file(store, "composer.json")

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        result = self._find_file(store, "composer.json")
        data: dict = {}
        if result:
            try:
                data = json.loads(result[1])
            except json.JSONDecodeError:
                pass

        return ManifestInfo(
            language="php",
            runtime_version=self._extract_php_version(data),
            dependencies=data.get("require", {}),
            dev_dependencies=data.get("require-dev", {}),
            scripts=data.get("scripts", {}),
            pkg_manager="composer",
            pkg_manager_version=None,
            entry_point=self._find_entry(store),
            detected_port=self._port_detector.detect(store),
            raw_deps=list(data.get("require", {}).keys()),
        )

    @staticmethod
    def _extract_php_version(data: dict) -> str | None:
        php_req = data.get("require", {}).get("php", "")
        import re
        m = re.search(r"(\d+\.\d+)", php_req)
        return m.group(1) if m else None

    @staticmethod
    def _find_entry(store: dict[str, str]) -> str | None:
        for path in store.keys():
            from pathlib import Path
            if Path(path).name in ("index.php", "artisan", "public/index.php"):
                return path
        return None
