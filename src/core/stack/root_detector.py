from typing import Optional

from src.infra.llm.dockerfile_processing.constants import STACK_PATTERNS
from src.infra.llm.dockerfile_processing.paths import _normalize_source_path


class ProjectRootDetector:
    _ROOT_INDICATORS = {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "go.mod",
        "Cargo.toml",
        "Gemfile",
        "composer.json",
        "Pipfile",
    }

    def detect(self, store: dict[str, str], stack: Optional[str]) -> str:
        indicators = set(self._ROOT_INDICATORS)
        if stack and stack in STACK_PATTERNS:
            config = STACK_PATTERNS[stack]
            for f in config.get("detector", []) + config.get("secondary", []):
                if "/" not in f:
                    indicators.add(f)

        candidate_dirs: dict[str, int] = {}
        for path in store.keys():
            normalized = _normalize_source_path(path)
            filename = normalized.split("/")[-1] if "/" in normalized else normalized
            if filename not in indicators:
                continue
            parent = "/".join(normalized.split("/")[:-1]) if "/" in normalized else ""
            depth = len(parent.split("/")) if parent else 0
            if parent not in candidate_dirs or depth < candidate_dirs[parent]:
                candidate_dirs[parent] = depth

        if not candidate_dirs:
            return ""

        shallowest = min(candidate_dirs.items(), key=lambda x: x[1])[0]
        return (shallowest + "/") if shallowest else ""
