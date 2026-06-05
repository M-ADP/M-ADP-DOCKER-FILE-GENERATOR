import json
import logging
import re
from pathlib import Path
from typing import Literal

from src.core.manifest.models import ManifestInfo
from src.core.params.models import DetectedParams

logger = logging.getLogger(__name__)

_FRAMEWORK_RUNTIME: dict[str, Literal["interpreted", "compiled", "static"]] = {
    "nextjs":        "interpreted",
    "nuxt":          "interpreted",
    "node-server":   "interpreted",
    "python-fastapi":"interpreted",
    "python-flask":  "interpreted",
    "python":        "interpreted",
    "ruby":          "interpreted",
    "php":           "interpreted",
    "go":            "compiled",
    "java-gradle":   "compiled",
    "java-maven":    "compiled",
    "rust":          "compiled",
    "astro":         "static",
    "vue":           "static",
    "svelte":        "static",
    "vite-static":   "static",
    "node-static":   "static",
}

_ROOT_INDICATORS = frozenset({
    "package.json", "requirements.txt", "pyproject.toml",
    "pom.xml", "build.gradle", "build.gradle.kts",
    "go.mod", "Cargo.toml", "Gemfile", "composer.json", "Pipfile",
})

# 프레임워크 → 해당 package.json에 반드시 있어야 하는 의존성
_FRAMEWORK_KEY_DEP: dict[str, str] = {
    "nextjs":      "next",
    "nuxt":        "nuxt",
    "astro":       "astro",
    "svelte":      "@sveltejs/kit",
    "vue":         "vue",
    "vite-static": "vite",
}

_PYTHON_ENTRY_CANDIDATES = ("main.py", "app.py", "application.py", "run.py", "server.py", "wsgi.py", "asgi.py")
_NODE_ENTRY_CANDIDATES   = ("index.js", "index.ts", "server.js", "server.ts", "app.js", "app.ts", "main.js", "main.ts")


class Analyzer:
    def analyze(self, store: dict[str, str], manifest: ManifestInfo) -> DetectedParams:
        norm = {self._norm(k): v for k, v in store.items()}
        files = {Path(k).name for k in norm}

        pkg_jsons_with_paths = self._load_package_jsons_with_paths(norm)
        pkg_jsons            = [p for _, p in pkg_jsons_with_paths]
        framework            = self._detect_framework(norm, files, pkg_jsons)
        runtime              = _FRAMEWORK_RUNTIME.get(framework) if framework else None
        # 프레임워크 키 의존성이 있는 package.json 위치를 우선 사용 (모노레포 대응)
        project_root = self._detect_root_for_framework(norm, framework, pkg_jsons_with_paths)
        pkg_manager, lockfile = self._detect_pkg_manager(norm, files, project_root)
        standalone   = self._detect_standalone(norm)
        req_file     = self._detect_req_file(norm, files, project_root)
        entry_point  = self._detect_entry_point(norm, files, project_root, framework)

        detected = DetectedParams(
            project_root     = project_root,
            framework        = framework,
            runtime          = runtime,
            package_manager  = pkg_manager,
            lockfile         = lockfile,
            standalone       = standalone,
            req_file         = req_file,
            has_public_dir   = any(k.startswith(f"{project_root}public/") for k in norm),
            has_go_sum       = "go.sum" in files,
            has_yarnrc       = f"{project_root}.yarnrc.yml" in norm or ".yarnrc.yml" in files,
            has_yarn_releases= any(k.startswith(f"{project_root}.yarn/releases/") for k in norm),
            entry_point      = entry_point,
        )
        logger.info(
            "[Analyzer] framework=%s runtime=%s pkg=%s root='%s' standalone=%s",
            detected.framework, detected.runtime, detected.package_manager,
            detected.project_root, detected.standalone,
        )
        return detected

    # ── 헬퍼 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _norm(path: str) -> str:
        p = path.replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        return p.lstrip("/")

    def _detect_root(self, norm: dict[str, str]) -> str:
        candidates: dict[str, int] = {}
        for path in norm:
            name = Path(path).name
            if name not in _ROOT_INDICATORS:
                continue
            parent = str(Path(path).parent)
            parent = "" if parent == "." else parent + "/"
            depth  = parent.count("/") if parent else 0
            if parent not in candidates or depth < candidates[parent]:
                candidates[parent] = depth
        if not candidates:
            return ""
        return min(candidates, key=candidates.get)  # type: ignore[arg-type]

    @staticmethod
    def _load_package_jsons_with_paths(norm: dict[str, str]) -> list[tuple[str, dict]]:
        result = []
        for path, content in norm.items():
            if Path(path).name != "package.json":
                continue
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    result.append((path, data))
            except json.JSONDecodeError:
                pass
        return result

    @staticmethod
    def _load_package_jsons(norm: dict[str, str]) -> list[dict]:
        result = []
        for path, content in norm.items():
            if Path(path).name != "package.json":
                continue
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    result.append(data)
            except json.JSONDecodeError:
                pass
        return result

    def _detect_root_for_framework(
        self,
        norm: dict[str, str],
        framework: str | None,
        pkg_jsons_with_paths: list[tuple[str, dict]],
    ) -> str:
        """모노레포: 프레임워크 키 의존성이 있는 package.json 디렉토리를 project_root로 사용."""
        key_dep = _FRAMEWORK_KEY_DEP.get(framework or "")
        if key_dep and pkg_jsons_with_paths:
            for path, pkg in pkg_jsons_with_paths:
                all_deps = {
                    **pkg.get("dependencies", {}),
                    **pkg.get("devDependencies", {}),
                }
                if key_dep in all_deps:
                    parent = str(Path(path).parent)
                    root = "" if parent == "." else parent + "/"
                    logger.info(
                        "[Analyzer] monorepo root: '%s' (found '%s' in %s)", root, key_dep, path
                    )
                    return root
        return self._detect_root(norm)

    def _detect_framework(
        self,
        norm: dict[str, str],
        files: set[str],
        pkg_jsons: list[dict],
    ) -> str | None:
        deps:    set[str] = set()
        scripts: set[str] = set()
        for pkg in pkg_jsons:
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                deps.update((pkg.get(section) or {}).keys())
            scripts.update((pkg.get("scripts") or {}).keys())

        def has_dep(*names: str)  -> bool: return any(n in deps for n in names)
        def has_file(*names: str) -> bool: return any(n in files for n in names)

        # Node 스택 (우선순위 순)
        if has_dep("next") or has_file("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs"):
            return "nextjs"
        if has_dep("nuxt") or has_file("nuxt.config.js", "nuxt.config.ts"):
            return "nuxt"
        if has_dep("astro") or has_file("astro.config.js", "astro.config.mjs", "astro.config.ts"):
            return "astro"
        if has_dep("@sveltejs/kit") or has_dep("svelte") or has_file("svelte.config.js", "svelte.config.ts"):
            return "svelte"
        if has_dep("vue") or has_file("vue.config.js", "vue.config.ts"):
            return "vue"
        if has_dep("vite") or has_file("vite.config.js", "vite.config.ts"):
            return "vite-static"
        if has_dep("express", "fastify", "koa", "@nestjs/core", "hapi", "@hapi/hapi"):
            return "node-server"
        if pkg_jsons and "build" in scripts:
            return "node-static"
        if pkg_jsons:
            return "node-server"

        # Python 스택
        is_python = has_file("requirements.txt", "pyproject.toml", "Pipfile", "setup.py", "setup.cfg")
        if not is_python:
            is_python = any(Path(p).name in _PYTHON_ENTRY_CANDIDATES for p in files)
        if is_python:
            content_all = " ".join(norm.values()).lower()
            if "fastapi" in content_all:
                return "python-fastapi"
            if "flask" in content_all:
                return "python-flask"
            return "python"

        # 컴파일 스택
        if has_file("go.mod"):
            return "go"
        if has_file("build.gradle", "build.gradle.kts", "gradlew"):
            return "java-gradle"
        if has_file("pom.xml"):
            return "java-maven"
        if has_file("Cargo.toml"):
            return "rust"
        if has_file("Gemfile"):
            return "ruby"
        if has_file("composer.json"):
            return "php"

        return None

    def _detect_pkg_manager(
        self,
        norm: dict[str, str],
        files: set[str],
        root: str,
    ) -> tuple[str, str | None]:
        # lockfile 우선 (certain)
        if f"{root}pnpm-lock.yaml" in norm or "pnpm-lock.yaml" in files:
            return "pnpm", "pnpm-lock.yaml"
        if f"{root}bun.lockb" in norm or "bun.lockb" in files:
            return "bun", "bun.lockb"
        if f"{root}yarn.lock" in norm or "yarn.lock" in files:
            if f"{root}.yarnrc.yml" in norm or ".yarnrc.yml" in files:
                return "yarn-berry", "yarn.lock"
            return "yarn", "yarn.lock"
        if f"{root}package-lock.json" in norm or "package-lock.json" in files:
            return "npm", "package-lock.json"
        if "package.json" in files:
            return "npm", None

        # 비Node
        if "requirements.txt" in files or "pyproject.toml" in files or "Pipfile" in files:
            return "pip", None
        if "build.gradle" in files or "build.gradle.kts" in files:
            return "gradle", None
        if "pom.xml" in files:
            return "maven", None
        if "go.mod" in files:
            return "go", None
        if "Cargo.toml" in files:
            return "cargo", None
        if "Gemfile" in files:
            return "bundler", None
        if "composer.json" in files:
            return "composer", None

        return "unknown", None

    @staticmethod
    def _detect_standalone(norm: dict[str, str]) -> bool:
        config_names = {"next.config.ts", "next.config.js", "next.config.mjs", "next.config.cjs"}
        for k, v in norm.items():
            if Path(k).name in config_names:
                if re.search(r"""output\s*:\s*['"]standalone['"]""", v):
                    return True
        return False

    @staticmethod
    def _detect_req_file(norm: dict[str, str], files: set[str], root: str) -> str | None:
        if f"{root}requirements.txt" in norm or "requirements.txt" in files:
            return "requirements.txt"
        if f"{root}pyproject.toml" in norm or "pyproject.toml" in files:
            return "pyproject.toml"
        if f"{root}Pipfile" in norm or "Pipfile" in files:
            return "Pipfile"
        return None

    @staticmethod
    def _detect_entry_point(
        norm: dict[str, str],
        files: set[str],
        root: str,
        framework: str | None,
    ) -> str | None:
        candidates = (
            _PYTHON_ENTRY_CANDIDATES
            if framework and "python" in framework
            else _NODE_ENTRY_CANDIDATES
        )
        for c in candidates:
            # 프로젝트 루트에 바로 있으면 파일명만 반환
            if f"{root}{c}" in norm:
                return c
            # 서브디렉토리에 있으면 project_root 기준 상대 경로 반환
            matches = [p for p in norm if p.startswith(root) and Path(p).name == c]
            if matches:
                shallowest = min(matches, key=lambda x: x.count("/"))
                return shallowest[len(root):]
        return None
