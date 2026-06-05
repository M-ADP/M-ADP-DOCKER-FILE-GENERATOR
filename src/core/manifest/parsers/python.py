import re
import tomllib

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector


class PythonManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    _MANIFEST_FILES = ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py", "setup.cfg")
    _ENTRY_CANDIDATES = frozenset(("main.py", "app.py", "wsgi.py", "asgi.py", "server.py", "run.py"))

    def can_handle(self, store: dict[str, str]) -> bool:
        from pathlib import Path
        if self._has_file(store, *self._MANIFEST_FILES):
            return True
        if any(Path(p).name in self._ENTRY_CANDIDATES for p in store):
            return True
        return any(p.endswith(".py") for p in store)

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        runtime_version = self._extract_python_version(store)
        raw_deps = self._extract_deps(store)
        entry_point = self._find_entry_point(store)

        return ManifestInfo(
            language="python",
            runtime_version=runtime_version,
            dependencies={},
            dev_dependencies={},
            scripts={},
            pkg_manager="pip",
            pkg_manager_version=None,
            entry_point=entry_point,
            detected_port=self._port_detector.detect(store),
            raw_deps=raw_deps,
        )

    def _extract_python_version(self, store: dict[str, str]) -> str | None:
        result = self._find_file(store, "pyproject.toml")
        if result:
            try:
                data = tomllib.loads(result[1])
                requires = (
                    data.get("project", {}).get("requires-python", "")
                    or data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python", "")
                )
                m = re.search(r"(\d+\.\d+)", requires)
                if m:
                    return m.group(1)
            except Exception:
                pass

        result = self._find_file(store, ".python-version")
        if result:
            return result[1].strip()

        return None

    def _extract_deps(self, store: dict[str, str]) -> list[str]:
        result = self._find_file(store, "requirements.txt")
        if result:
            return [
                line.strip()
                for line in result[1].splitlines()
                if line.strip() and not line.startswith("#")
            ]

        result = self._find_file(store, "pyproject.toml")
        if result:
            try:
                data = tomllib.loads(result[1])
                deps = data.get("project", {}).get("dependencies", [])
                if isinstance(deps, list):
                    return deps
            except Exception:
                pass

        return []

    def _find_entry_point(self, store: dict[str, str]) -> str | None:
        from pathlib import Path
        for path in store.keys():
            if Path(path).name in self._ENTRY_CANDIDATES:
                return path
        return None
