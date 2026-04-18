import re
import xml.etree.ElementTree as ET

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector

_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


class JavaMavenManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    def can_handle(self, store: dict[str, str]) -> bool:
        return self._has_file(store, "pom.xml")

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        result = self._find_file(store, "pom.xml")
        content = result[1] if result else ""

        return ManifestInfo(
            language="java",
            runtime_version=self._extract_java_version(content),
            dependencies={},
            dev_dependencies={},
            scripts={},
            pkg_manager="maven",
            pkg_manager_version=None,
            entry_point=None,
            detected_port=self._port_detector.detect(store),
            raw_deps=self._extract_deps(content),
            extra={"build_tool": "maven", "is_spring": self._is_spring(content)},
        )

    @staticmethod
    def _extract_java_version(content: str) -> str | None:
        try:
            root = ET.fromstring(content)
            for tag in ("maven.compiler.source", "java.version"):
                el = root.find(f".//*[local-name()='properties']/*[local-name()='{tag}']")
                if el is not None and el.text:
                    m = re.search(r"(\d+)", el.text)
                    return m.group(1) if m else None
        except ET.ParseError:
            pass
        return None

    @staticmethod
    def _extract_deps(content: str) -> list[str]:
        deps: list[str] = []
        try:
            root = ET.fromstring(content)
            for dep in root.findall(".//*[local-name()='dependency']"):
                g = dep.find("*[local-name()='groupId']")
                a = dep.find("*[local-name()='artifactId']")
                if g is not None and a is not None and g.text and a.text:
                    deps.append(f"{g.text}:{a.text}")
        except ET.ParseError:
            pass
        return deps

    @staticmethod
    def _is_spring(content: str) -> bool:
        return "spring-boot" in content.lower()
