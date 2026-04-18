import json
import re
from pathlib import Path

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.port_detector import PortDetector


class NodeManifestParser(BaseManifestParser):
    def __init__(self, port_detector: PortDetector) -> None:
        self._port_detector = port_detector

    def can_handle(self, store: dict[str, str]) -> bool:
        return self._has_file(store, "package.json")

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        result = self._find_file(store, "package.json")
        pkg: dict = {}
        if result:
            try:
                pkg = json.loads(result[1])
            except json.JSONDecodeError:
                pass

        pkg_manager, pkg_manager_version = self._resolve_pkg_manager(pkg, store)
        runtime_version = self._extract_node_version(pkg.get("engines", {}).get("node", ""))

        return ManifestInfo(
            language="node",
            runtime_version=runtime_version,
            dependencies=pkg.get("dependencies") or {},
            dev_dependencies=pkg.get("devDependencies") or {},
            scripts=pkg.get("scripts") or {},
            pkg_manager=pkg_manager,
            pkg_manager_version=pkg_manager_version,
            entry_point=pkg.get("main") or pkg.get("module"),
            detected_port=self._port_detector.detect(store),
            raw_deps=[],
            extra={"workspaces": pkg.get("workspaces", [])},
        )

    def _resolve_pkg_manager(
        self, pkg: dict, store: dict[str, str]
    ) -> tuple[str, str | None]:
        field = pkg.get("packageManager", "")
        if field:
            name, _, version = field.partition("@")
            return name.lower(), version or None

        return self._detect_from_lockfile(store), None

    @staticmethod
    def _detect_from_lockfile(store: dict[str, str]) -> str:
        files = {Path(p).name for p in store.keys()}
        has_yarnrc = any(
            Path(p).name in (".yarnrc.yml", ".yarnrc.yaml") for p in store.keys()
        )
        if has_yarnrc:
            return "yarn-berry"
        if "yarn.lock" in files:
            return "yarn"
        if "pnpm-lock.yaml" in files:
            return "pnpm"
        if "bun.lockb" in files or "bun.lock" in files:
            return "bun"
        return "npm"

    @staticmethod
    def _extract_node_version(engines_node: str) -> str | None:
        m = re.search(r"(\d+)", engines_node)
        return m.group(1) if m else None
