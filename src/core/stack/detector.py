import json
from pathlib import Path
from typing import Optional

from src.infra.llm.dockerfile_processing.constants import STACK_PATTERNS


class StackDetector:
    def detect(self, store: dict[str, str]) -> Optional[str]:
        normalized_paths = [p.replace("\\", "/").lstrip("./") for p in store.keys()]
        files = {Path(p).name for p in normalized_paths}

        package_jsons = self._load_package_jsons(store)
        package_names = self._collect_package_names(package_jsons)
        package_scripts = self._collect_scripts(package_jsons)

        scores: dict[str, int] = {}

        self._score_node_stacks(scores, files, package_names, package_scripts, package_jsons)
        self._score_other_stacks(scores, files)

        return max(scores.items(), key=lambda x: x[1])[0] if scores else None

    @staticmethod
    def _load_package_jsons(store: dict[str, str]) -> list[dict]:
        result = []
        for path, content in store.items():
            if Path(path).name != "package.json":
                continue
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    result.append(parsed)
            except json.JSONDecodeError:
                pass
        return result

    @staticmethod
    def _collect_package_names(package_jsons: list[dict]) -> set[str]:
        names: set[str] = set()
        for pkg in package_jsons:
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                section_data = pkg.get(section, {})
                if isinstance(section_data, dict):
                    names.update(section_data.keys())
        return names

    @staticmethod
    def _collect_scripts(package_jsons: list[dict]) -> set[str]:
        scripts: set[str] = set()
        for pkg in package_jsons:
            s = pkg.get("scripts", {})
            if isinstance(s, dict):
                scripts.update(s.keys())
        return scripts

    @staticmethod
    def _score_node_stacks(
        scores: dict[str, int],
        files: set[str],
        package_names: set[str],
        package_scripts: set[str],
        package_jsons: list[dict],
    ) -> None:
        def has_pkg(*names: str) -> bool:
            return any(n in package_names for n in names)

        def has_file(*names: str) -> bool:
            return any(n in files for n in names)

        def add(stack: str, score: int) -> None:
            scores[stack] = scores.get(stack, 0) + score

        if has_pkg("next") or has_file("next.config.js", "next.config.mjs", "next.config.ts"):
            add("nextjs", 120)
        if has_pkg("nuxt") or has_file("nuxt.config.js", "nuxt.config.ts"):
            add("nuxt", 120)
        if has_pkg("astro") or has_file("astro.config.js", "astro.config.mjs", "astro.config.ts"):
            add("astro", 110)
        if has_pkg("@sveltejs/kit", "svelte") or has_file("svelte.config.js", "svelte.config.ts"):
            add("svelte", 100)
        if has_pkg("vue") or has_file("vue.config.js", "vue.config.ts"):
            add("vue", 95)
        if has_pkg("vite") or has_file("vite.config.js", "vite.config.ts"):
            add("vite-static", 80)
        if has_pkg("express", "fastify", "koa", "@nestjs/core", "hapi"):
            add("node-server", 85)
        if package_jsons and "build" in package_scripts:
            add("node-static", 45)
        elif package_jsons:
            add("node-server", 35)

    @staticmethod
    def _score_other_stacks(scores: dict[str, int], files: set[str]) -> None:
        node_stacks = {
            "nextjs", "nuxt", "vue", "svelte", "astro",
            "vite-static", "node-static", "node-server",
        }
        for stack_name, config in STACK_PATTERNS.items():
            if stack_name in node_stacks:
                continue
            for detector in config.get("detector", []):
                if detector in files:
                    scores[stack_name] = scores.get(stack_name, 0) + config["score"]
            for secondary in config.get("secondary", []):
                if secondary in files and config.get("detector"):
                    scores[stack_name] = (
                        scores.get(stack_name, 0) + config["score"] // 4
                    )
