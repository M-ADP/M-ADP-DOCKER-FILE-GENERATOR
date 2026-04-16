from pathlib import Path
from typing import Optional

from src.infra.llm.dockerfile_processing.constants import BUILD_REQUIRED_PATHS


def _normalize_source_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _is_build_required_path(path: str) -> bool:
    normalized = _normalize_source_path(path)
    basename = Path(normalized).name

    if normalized in BUILD_REQUIRED_PATHS or basename in BUILD_REQUIRED_PATHS:
        return True

    return any(
        normalized.endswith(f"/{required}") for required in BUILD_REQUIRED_PATHS
    )


def _is_available_source(path: str, available_paths: set[str]) -> bool:
    normalized = _normalize_source_path(path)
    normalized_available_paths = {
        _normalize_source_path(available) for available in available_paths
    }
    return normalized in normalized_available_paths


def _resolve_store_path(store: dict[str, str], path: str) -> Optional[str]:
    normalized = _normalize_source_path(path)
    normalized_paths = {
        _normalize_source_path(store_path): store_path for store_path in store.keys()
    }
    if normalized in normalized_paths:
        return normalized_paths[normalized]

    matches = [
        store_path
        for store_path in store.keys()
        if Path(_normalize_source_path(store_path)).name == normalized
    ]
    if len(matches) == 1:
        return matches[0]

    return None


def _build_tree_from_store(
    store: dict[str, str],
    path: str = "",
    max_depth: int = 3,
) -> str:
    root = _normalize_source_path(path)
    max_depth = max(1, min(max_depth, 8))
    entries: set[tuple[str, bool]] = set()

    for store_path in store.keys():
        normalized = _normalize_source_path(store_path)
        if root:
            if normalized == root:
                entries.add((Path(normalized).name, False))
                continue
            prefix = f"{root}/"
            if not normalized.startswith(prefix):
                continue
            relative = normalized[len(prefix) :]
        else:
            relative = normalized

        parts = [part for part in Path(relative).parts if part]
        for depth, part in enumerate(parts[:max_depth], 1):
            is_file = depth == len(parts)
            display = "/".join(parts[:depth])
            entries.add((display, is_file))

    if not entries:
        return f"[오류] 디렉토리 또는 파일을 찾을 수 없습니다: {path or '.'}"

    lines = [f"{root or '.'}/"]
    for entry, is_file in sorted(entries):
        depth = len(Path(entry).parts)
        indent = "  " * depth
        suffix = "" if is_file else "/"
        lines.append(f"{indent}{Path(entry).name}{suffix}")

    return "\n".join(lines)
