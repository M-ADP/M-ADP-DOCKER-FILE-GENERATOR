import re
import tomllib

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector


class RustManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    def can_handle(self, store: dict[str, str]) -> bool:
        return self._has_file(store, "Cargo.toml")

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        result = self._find_file(store, "Cargo.toml")
        data: dict = {}
        if result:
            try:
                data = tomllib.loads(result[1])
            except Exception:
                pass

        return ManifestInfo(
            language="rust",
            runtime_version=self._extract_rust_edition(data),
            dependencies={k: str(v) for k, v in data.get("dependencies", {}).items()},
            dev_dependencies={
                k: str(v) for k, v in data.get("dev-dependencies", {}).items()
            },
            scripts={},
            pkg_manager="cargo",
            pkg_manager_version=None,
            entry_point="src/main.rs" if self._has_file(store, "main.rs") else None,
            detected_port=self._port_detector.detect(store),
            raw_deps=list(data.get("dependencies", {}).keys()),
            extra={"bin_name": data.get("package", {}).get("name")},
        )

    @staticmethod
    def _extract_rust_edition(data: dict) -> str | None:
        return data.get("package", {}).get("edition")
