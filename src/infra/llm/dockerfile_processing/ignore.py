import logging
import shlex
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

from src.infra.llm.dockerfile_processing.constants import (
    DOCKERIGNORE_COMMON,
    DOCKERIGNORE_JAVA_GRADLE,
    DOCKERIGNORE_JAVA_MAVEN,
    DOCKERIGNORE_NODE,
    DOCKERIGNORE_PYTHON,
    OPTIONAL_NODE_COPY_FILES,
)
from src.infra.llm.dockerfile_processing.copy import _parse_copy_sources
from src.infra.llm.dockerfile_processing.paths import (
    _is_available_source,
    _is_build_required_path,
    _normalize_source_path,
)

logger = logging.getLogger(__name__)


def _normalize_dockerignore_pattern(pattern: str) -> str:
    return _normalize_source_path(pattern)


def _dockerignore_pattern_matches_path(pattern: str, path: str) -> bool:
    normalized_pattern = _normalize_dockerignore_pattern(pattern)
    normalized_path = _normalize_source_path(path)

    if (
        not normalized_pattern
        or normalized_pattern.startswith("#")
        or normalized_pattern.startswith("!")
    ):
        return False

    if normalized_pattern.endswith("/"):
        directory = normalized_pattern.rstrip("/")
        return (
            normalized_path == directory
            or normalized_path.startswith(f"{directory}/")
            or Path(normalized_path).name == directory
        )

    basename = Path(normalized_path).name
    if normalized_pattern == normalized_path or normalized_pattern == basename:
        return True

    if "/" in normalized_pattern:
        return fnmatch(normalized_path, normalized_pattern)

    return fnmatch(basename, normalized_pattern)


def _pattern_excludes_required_file(pattern: str, required_paths: set[str]) -> bool:
    for required_path in required_paths:
        if _dockerignore_pattern_matches_path(pattern, required_path):
            return True

    return False


def _filter_build_required_patterns(lines: list[str], store: dict[str, str]) -> list[str]:
    available_required_paths = {
        _normalize_source_path(path)
        for path in store.keys()
        if _is_build_required_path(path)
    }
    if not available_required_paths:
        return lines

    filtered: list[str] = []
    for line in lines:
        if _pattern_excludes_required_file(line, available_required_paths):
            logger.warning(
                "[Dockerignore] Removed build-required exclusion pattern: %s",
                line,
            )
            continue
        filtered.append(line)

    return filtered


def _remove_missing_optional_copy_sources(
    dockerfile: str,
    store: dict[str, str],
) -> str:
    available_paths = set(store.keys())
    lines = dockerfile.split("\n")
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("COPY ") or "--from=" in stripped:
            result.append(line)
            continue

        try:
            tokens = shlex.split(stripped)
        except ValueError:
            result.append(line)
            continue

        if len(tokens) < 3 or tokens[0] != "COPY":
            result.append(line)
            continue

        option_tokens: list[str] = []
        source_start = 1
        while source_start < len(tokens) and tokens[source_start].startswith("--"):
            option_tokens.append(tokens[source_start])
            source_start += 1

        sources = tokens[source_start:-1]
        destination = tokens[-1]
        if not sources:
            result.append(line)
            continue

        kept_sources: list[str] = []
        removed_sources: list[str] = []
        for source in sources:
            source_path = _normalize_source_path(source)
            source_name = Path(source_path).name
            is_optional_node_file = (
                source_path in OPTIONAL_NODE_COPY_FILES
                or source_name in OPTIONAL_NODE_COPY_FILES
            )
            if is_optional_node_file and not _is_available_source(
                source,
                available_paths,
            ):
                removed_sources.append(source)
                continue
            kept_sources.append(source)

        if not removed_sources:
            result.append(line)
            continue

        logger.warning(
            "[Dockerfile] Removed missing optional COPY sources: %s",
            ", ".join(removed_sources),
        )
        if not kept_sources:
            continue

        indent = line[: len(line) - len(line.lstrip())]
        result.append(
            " ".join([f"{indent}COPY", *option_tokens, *kept_sources, destination])
        )

    return "\n".join(result)


def _reconcile_dockerignore_with_dockerfile(
    dockerignore: str,
    dockerfile: str,
) -> str:
    copied_sources = {
        _normalize_source_path(source)
        for line in dockerfile.splitlines()
        for source in _parse_copy_sources(line)
        if source and source not in {".", "./"}
    }
    if not copied_sources:
        return dockerignore

    result: list[str] = []
    for line in dockerignore.splitlines():
        if any(
            _dockerignore_pattern_matches_path(line, copied_source)
            for copied_source in copied_sources
        ):
            logger.warning(
                "[Dockerignore] Removed pattern conflicting with Dockerfile COPY: %s",
                line,
            )
            continue
        result.append(line)

    return "\n".join(result)


def generate_dockerignore(store: dict[str, str], stack: Optional[str]) -> str:
    lines = list(DOCKERIGNORE_COMMON)

    files = set(Path(p).name for p in store.keys())
    dirs = set()
    for p in store.keys():
        parts = Path(p).parts
        if len(parts) > 1:
            dirs.update(parts[:-1])

    if stack and stack.startswith("node"):
        for pattern in DOCKERIGNORE_NODE:
            lines.append(pattern)
    elif stack and stack.startswith("python"):
        for pattern in DOCKERIGNORE_PYTHON:
            lines.append(pattern)
    elif stack == "java-gradle":
        for pattern in DOCKERIGNORE_JAVA_GRADLE:
            lines.append(pattern)
    elif stack == "java-maven":
        for pattern in DOCKERIGNORE_JAVA_MAVEN:
            lines.append(pattern)
    else:
        if "node_modules" in dirs or "package.json" in files:
            lines.extend(DOCKERIGNORE_NODE)
        if (
            "__pycache__" in dirs
            or "requirements.txt" in files
            or "pyproject.toml" in files
        ):
            lines.extend(DOCKERIGNORE_PYTHON)
        if "build.gradle" in files or "build.gradle.kts" in files or "gradle" in dirs:
            lines.extend(DOCKERIGNORE_JAVA_GRADLE)
        if "pom.xml" in files:
            lines.extend(DOCKERIGNORE_JAVA_MAVEN)

    if ".dockerignore" in files:
        lines.insert(0, "# 기존 .dockerignore 참고하여 생성됨")

    lines = _filter_build_required_patterns(lines, store)
    return "\n".join(lines)
