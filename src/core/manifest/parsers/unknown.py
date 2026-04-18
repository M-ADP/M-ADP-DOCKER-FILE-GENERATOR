from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector


class UnknownManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    def can_handle(self, store: dict[str, str]) -> bool:
        return True

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        return ManifestInfo(
            language="unknown",
            runtime_version=None,
            dependencies={},
            dev_dependencies={},
            scripts={},
            pkg_manager="unknown",
            pkg_manager_version=None,
            entry_point=None,
            detected_port=self._port_detector.detect(store),
            raw_deps=[],
        )
