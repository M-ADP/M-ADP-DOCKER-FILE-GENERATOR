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
            for elem in root.iter():
                local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if local == "properties":
                    for child in elem:
                        child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        if child_local in ("maven.compiler.source", "java.version") and child.text:
                            m = re.search(r"(\d+)", child.text)
                            return m.group(1) if m else None
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_deps(content: str) -> list[str]:
        deps: list[str] = []
        try:
            root = ET.fromstring(content)
            for elem in root.iter():
                local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if local == "dependency":
                    g_text = a_text = None
                    for child in elem:
                        child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        if child_local == "groupId":
                            g_text = child.text
                        elif child_local == "artifactId":
                            a_text = child.text
                    if g_text and a_text:
                        deps.append(f"{g_text}:{a_text}")
        except Exception:
            pass
        return deps

    @staticmethod
    def _is_spring(content: str) -> bool:
        return "spring-boot" in content.lower()
