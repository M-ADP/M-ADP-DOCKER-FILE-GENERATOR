import re

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector


class JavaGradleManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    def can_handle(self, store: dict[str, str]) -> bool:
        return self._has_file(store, "build.gradle", "build.gradle.kts")

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        build_file = (
            self._find_file(store, "build.gradle.kts")
            or self._find_file(store, "build.gradle")
        )
        content = build_file[1] if build_file else ""

        return ManifestInfo(
            language="java",
            runtime_version=self._extract_java_version(content),
            dependencies={},
            dev_dependencies={},
            scripts={},
            pkg_manager="gradle",
            pkg_manager_version=self._extract_gradle_version(store),
            entry_point=None,
            detected_port=self._port_detector.detect(store),
            raw_deps=self._extract_deps(content),
            extra={"build_tool": "gradle", "is_spring": self._is_spring(content)},
        )

    @staticmethod
    def _extract_java_version(content: str) -> str | None:
        m = re.search(r"sourceCompatibility\s*=\s*['\"]?(\d+)['\"]?", content)
        if m:
            return m.group(1)
        m = re.search(r"JavaVersion\.VERSION_(\d+)", content)
        return m.group(1) if m else None

    @staticmethod
    def _extract_gradle_version(store: dict[str, str]) -> str | None:
        result = None
        for path, content in store.items():
            if "gradle-wrapper.properties" in path:
                result = content
                break
        if not result:
            return None
        m = re.search(r"gradle-(\d+\.\d+[\.\d]*)-", result)
        return m.group(1) if m else None

    @staticmethod
    def _extract_deps(content: str) -> list[str]:
        return re.findall(
            r"""(?:implementation|api|compileOnly|runtimeOnly)\s+['"]([^'"]+)['"]""",
            content,
        )

    @staticmethod
    def _is_spring(content: str) -> bool:
        return "spring-boot" in content.lower()
