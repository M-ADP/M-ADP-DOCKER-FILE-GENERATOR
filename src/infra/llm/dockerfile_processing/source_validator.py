import re
from typing import Optional

from src.infra.llm.dockerfile_processing.constants import NEXT_ROUTE_DIRS
from src.infra.llm.dockerfile_processing.copy import (
    _parse_copy_instruction,
    _parse_copy_sources,
)
from src.infra.llm.dockerfile_processing.paths import _normalize_source_path


def _is_node_build_command(line: str) -> bool:
    stripped = line.strip().lower()
    if not stripped.startswith("run "):
        return False

    return bool(
        re.search(
            r"\b(npm\s+run\s+build|yarn\s+build|pnpm\s+build|bun\s+run\s+build|next\s+build)\b",
            stripped,
        )
    )


def _copy_includes_application_source(line: str) -> bool:
    sources = _parse_copy_sources(line)
    known_source_dirs = {
        "src",
        "app",
        "pages",
        "components",
        "public",
    }
    for source in sources:
        normalized = _normalize_source_path(source)
        if normalized in {".", ""}:
            return True
        if normalized.rstrip("/") in known_source_dirs:
            return True
        if normalized.startswith(("src/", "app/", "pages/", "components/", "public/")):
            return True
        if "/" in normalized and not normalized.startswith("."):
            return True
    return False


def _logical_dockerfile_lines(dockerfile: str) -> list[tuple[int, str]]:
    logical_lines: list[tuple[int, str]] = []
    current = ""
    start_line = 1

    for line_no, line in enumerate(dockerfile.split("\n"), 1):
        stripped = line.strip()
        if not current:
            start_line = line_no

        if stripped.endswith("\\"):
            current += stripped[:-1].rstrip() + " "
            continue

        current += stripped
        logical_lines.append((start_line, current.strip()))
        current = ""

    if current:
        logical_lines.append((start_line, current.strip()))

    return logical_lines


def _join_image_path(base: str, path: str) -> str:
    normalized_base = _normalize_source_path(base)
    normalized_path = _normalize_source_path(path)
    if not normalized_path or normalized_path == ".":
        return normalized_base
    if path.strip().startswith("/"):
        return normalized_path
    if not normalized_base:
        return normalized_path
    return f"{normalized_base.rstrip('/')}/{normalized_path}"


def _discover_next_route_dirs(store: dict[str, str]) -> set[str]:
    route_dirs: set[str] = set()
    for store_path in store.keys():
        normalized = _normalize_source_path(store_path)
        parts = normalized.split("/")
        for route_dir in NEXT_ROUTE_DIRS:
            route_parts = route_dir.split("/")
            route_len = len(route_parts)
            for idx in range(0, len(parts) - route_len):
                if parts[idx : idx + route_len] == route_parts:
                    route_dirs.add("/".join(parts[: idx + route_len]))
    return route_dirs


def _source_maps_route_to_image_path(
    source: str,
    destination: str,
    workdir: str,
    route_dir: str,
) -> Optional[str]:
    normalized_source = _normalize_source_path(source).rstrip("/")
    normalized_route = _normalize_source_path(route_dir).rstrip("/")
    destination_base = _join_image_path(workdir, destination)

    if normalized_source in {"", "."}:
        return _join_image_path(destination_base, normalized_route)

    if normalized_route == normalized_source:
        return destination_base

    source_prefix = f"{normalized_source}/"
    if normalized_route.startswith(source_prefix):
        route_suffix = normalized_route[len(source_prefix) :]
        return _join_image_path(destination_base, route_suffix)

    return None


def _route_available_for_workdir(
    copied_route_paths: set[str],
    workdir: str,
) -> bool:
    normalized_workdir = _normalize_source_path(workdir)
    expected_paths = {
        _join_image_path(normalized_workdir, route_dir) for route_dir in NEXT_ROUTE_DIRS
    }
    return bool(copied_route_paths & expected_paths)


def _validate_package_manager_consistency(
    dockerfile: str,
    store: dict[str, str],
) -> list[str]:
    """Dockerfile에서 사용하는 패키지 매니저가 실제 소스 파일과 일치하는지 검증합니다."""
    issues = []
    normalized_paths = [p.replace("\\", "/").lstrip("./") for p in store.keys()]
    files = set(p.split("/")[-1] for p in normalized_paths)

    # Dockerfile에서 사용된 패키지 매니저 감지
    used_pnpm = False
    used_yarn = False
    used_npm = False
    used_bun = False

    for _, logical_line in _logical_dockerfile_lines(dockerfile):
        if not logical_line.startswith("RUN "):
            continue
        
        run_body = logical_line[4:].lower()
        if re.search(r"\bpnpm\b", run_body):
            used_pnpm = True
        if re.search(r"\byarn\b", run_body):
            used_yarn = True
        if re.search(r"\bnpm\b", run_body):
            used_npm = True
        if re.search(r"\bbun\b", run_body):
            used_bun = True

    # 실제 소스에 있는 lockfile 확인
    has_pnpm_lock = "pnpm-lock.yaml" in files
    has_yarn_lock = "yarn.lock" in files
    has_package_lock = "package-lock.json" in files
    has_bun_lock = "bun.lockb" in files or "bun.lock" in files

    if used_pnpm and not has_pnpm_lock:
        issues.append("Dockerfile uses pnpm but pnpm-lock.yaml is missing in the source")
    if used_yarn and not has_yarn_lock:
        issues.append("Dockerfile uses yarn but yarn.lock is missing in the source")
    if used_bun and not has_bun_lock:
        issues.append("Dockerfile uses bun but bun.lockb/bun.lock is missing in the source")
    
    # npm ci는 package-lock.json이 필수
    if "npm ci" in dockerfile and not has_package_lock:
        issues.append("Dockerfile uses 'npm ci' but package-lock.json is missing in the source")

    return issues


def _validate_dockerfile_against_source(
    dockerfile: str,
    store: dict[str, str],
    stack: Optional[str],
) -> list[str]:
    issues: list[str] = []
    
    # 패키지 매니저 정합성 검증 추가
    issues.extend(_validate_package_manager_consistency(dockerfile, store))

    if stack and not stack.startswith("node") and stack != "nextjs":
        return issues

    route_dirs = _discover_next_route_dirs(store)
    if not route_dirs:
        return []

    issues: list[str] = []
    workdir = ""
    copied_route_paths: set[str] = set()

    for line_no, logical_line in _logical_dockerfile_lines(dockerfile):
        if logical_line.startswith("FROM "):
            workdir = ""
            copied_route_paths.clear()
            continue

        if logical_line.startswith("WORKDIR "):
            workdir = _join_image_path(workdir, logical_line[8:].strip())
            continue

        copy_instruction = _parse_copy_instruction(logical_line)
        if copy_instruction:
            sources, destination = copy_instruction
            for source in sources:
                for route_dir in route_dirs:
                    image_route_path = _source_maps_route_to_image_path(
                        source,
                        destination,
                        workdir,
                        route_dir,
                    )
                    if image_route_path:
                        copied_route_paths.add(image_route_path)
            continue

        if _is_node_build_command(logical_line) and not _route_available_for_workdir(
            copied_route_paths,
            workdir,
        ):
            issues.append(
                f"Line {line_no}: Next.js build runs without app/pages route directory under WORKDIR {workdir or '/'}"
            )

    return issues


def _validate_copy_coverage(
    dockerfile: str,
    store: dict[str, str],
    project_root: str,
) -> list[str]:
    """프로젝트 루트 하위 파일이 Dockerfile COPY 커맨드로 커버되는지 검증합니다.

    project_root가 비어 있으면 검증을 건너뜁니다.
    """
    if not project_root:
        return []

    normalized_root = _normalize_source_path(project_root).rstrip("/")
    if not normalized_root:
        return []

    # 프로젝트 루트 하위 store 파일 수집
    files_under_root: set[str] = set()
    for path in store.keys():
        normalized = _normalize_source_path(path)
        if normalized == normalized_root or normalized.startswith(normalized_root + "/"):
            files_under_root.add(normalized)

    if not files_under_root:
        return []

    # Dockerfile에서 --from= 없는 COPY 소스 수집 (빌드 컨텍스트 COPY만)
    covered_sources: list[str] = []
    for _, logical_line in _logical_dockerfile_lines(dockerfile):
        copy_instruction = _parse_copy_instruction(logical_line)
        if copy_instruction:
            sources, _ = copy_instruction
            for source in sources:
                covered_sources.append(_normalize_source_path(source).rstrip("/"))

    def is_covered(file_path: str) -> bool:
        for src in covered_sources:
            if not src or src == ".":
                return True  # COPY . . → 전체 커버
            if src == file_path:
                return True  # 정확히 일치
            if file_path.startswith(src + "/"):
                return True  # 디렉토리 커버 (COPY pinball/ .)
        return False

    uncovered = sorted(f for f in files_under_root if not is_covered(f))
    if not uncovered:
        return []

    return [
        f"프로젝트 루트 '{normalized_root}/'의 파일이 COPY에서 누락되었습니다: "
        + ", ".join(uncovered[:5])
        + ("..." if len(uncovered) > 5 else "")
    ]


def _validate_dockerfile_syntax(dockerfile: str) -> tuple[bool, list[str]]:
    """Dockerfile 문법 검증 - 문제 패턴 감지"""
    issues = []
    lines = dockerfile.split("\n")

    in_continuation = False
    has_application_source_copy = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        if not stripped:
            in_continuation = False
            continue

        if in_continuation:
            if stripped.startswith(
                (
                    "FROM ",
                    "COPY ",
                    "WORKDIR ",
                    "ENV ",
                    "EXPOSE ",
                    "USER ",
                    "CMD ",
                    "ENTRYPOINT ",
                )
            ):
                issues.append(
                    f"Line {i}: Dockerfile instruction appears inside RUN continuation context"
                )
                in_continuation = False
            elif stripped.endswith("\\"):
                continue
            else:
                in_continuation = False
            continue

        if stripped.startswith("RUN "):
            if stripped.endswith("\\"):
                in_continuation = True
                continue
            run_body = stripped[4:].rstrip()
            if run_body.endswith("&&") or run_body.endswith("||"):
                issues.append(
                    f"Line {i}: RUN command ends with incomplete operator ({run_body[-2:]})"
                )
            if not run_body or run_body == "RUN":
                issues.append(f"Line {i}: RUN command has empty body")

        if stripped.startswith("COPY ") and "--from=" not in stripped:
            if _copy_includes_application_source(stripped):
                has_application_source_copy = True

        if stripped.startswith("FROM ") and i > 1:
            has_application_source_copy = False
            prev_idx = i - 2
            while prev_idx >= 0 and not lines[prev_idx].strip():
                prev_idx -= 1
            if prev_idx >= 0:
                prev = lines[prev_idx].strip()
                if prev.endswith("\\"):
                    issues.append(
                        f"Line {i}: FROM appears after RUN line ending with backslash"
                    )

    has_application_source_copy = False
    for line_no, logical_line in _logical_dockerfile_lines(dockerfile):
        if logical_line.startswith("FROM "):
            has_application_source_copy = False
            continue
        if logical_line.startswith("COPY ") and "--from=" not in logical_line:
            if _copy_includes_application_source(logical_line):
                has_application_source_copy = True
        if _is_node_build_command(logical_line) and not has_application_source_copy:
            issues.append(
                f"Line {line_no}: Node/Next build runs before application source COPY"
            )

    return len(issues) == 0, issues
