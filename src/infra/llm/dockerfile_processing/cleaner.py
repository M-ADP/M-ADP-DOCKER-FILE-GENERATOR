import logging
import re

from src.infra.llm.docker_hub_verifier import DockerHubVerifier

logger = logging.getLogger(__name__)


def _sanitize_base_images(dockerfile: str) -> str:
    verifier = DockerHubVerifier()
    lines = dockerfile.split("\n")
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("FROM "):
            result.append(line)
            continue

        rest = stripped[5:]
        image = rest.split(" AS ")[0].split(" as ")[0].strip()
        tag_lower = image.lower()

        replacement = verifier.get_replacement(tag_lower)
        if replacement:
            new_line = line.replace(image, replacement)
            logger.warning(
                f"[BaseImage] deprecated image replaced: {image} -> {replacement}"
            )
            result.append(new_line)
        else:
            result.append(line)

    return "\n".join(result)


VALID_DOCKERFILE_INSTRUCTIONS = {
    "FROM",
    "RUN",
    "CMD",
    "COPY",
    "ADD",
    "ENV",
    "EXPOSE",
    "WORKDIR",
    "USER",
    "ARG",
    "LABEL",
    "VOLUME",
    "MAINTAINER",
    "ENTRYPOINT",
    "ONBUILD",
    "STOPSIGNAL",
    "HEALTHCHECK",
    "SHELL",
}


def _normalize_continuation_lines(dockerfile: str) -> str:
    """RUN 명령어의 백슬래시 continuation을 단일 라인으로 정규화"""
    lines = dockerfile.split("\n")
    result: list[str] = []
    current_run: list[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("RUN "):
            if current_run:
                result.append("RUN " + " ".join(current_run))
                current_run = []

            cmd = stripped[4:]
            if cmd.endswith("\\"):
                cmd = cmd[:-1].rstrip()
                current_run.append(cmd)
            else:
                result.append(line)
        elif current_run:
            if stripped and not stripped.startswith(
                (
                    "FROM ",
                    "COPY ",
                    "WORKDIR ",
                    "ENV ",
                    "EXPOSE ",
                    "USER ",
                    "CMD ",
                    "ENTRYPOINT ",
                    "#",
                )
            ):
                if stripped.endswith("\\"):
                    current_run.append(stripped[:-1].rstrip())
                else:
                    current_run.append(stripped)
                    result.append("RUN " + " ".join(current_run))
                    current_run = []
            else:
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
                    result.append("RUN " + " ".join(current_run))
                    current_run = []
                    result.append(line)
                else:
                    result.append("RUN " + " ".join(current_run))
                    current_run = []
                    result.append(line)
        else:
            result.append(line)

    if current_run:
        result.append("RUN " + " ".join(current_run))

    return "\n".join(result)


def _remove_invalid_lines(dockerfile: str) -> str:
    """Dockerfile 명령어 형식에 맞지 않는 라인 제거"""
    lines = dockerfile.split("\n")
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue

        # FROM 라인은 항상 유지
        if stripped.startswith("FROM ") or stripped.startswith("FROM\t"):
            result.append(line)
            continue

        # 다른 유효한 명령어 확인
        first_word = stripped.split()[0].upper() if stripped.split() else ""

        if first_word in VALID_DOCKERFILE_INSTRUCTIONS:
            result.append(line)
            continue

        # 한글이 포함된 라인 제거
        if re.search(r"[가-힣]", stripped):
            logger.warning(f"[Dockerfile] Removed Korean line: {stripped[:50]}...")
            continue

        # 유효하지 않은 라인 제거
        logger.warning(f"[Dockerfile] Removed invalid line: {stripped[:50]}...")

    return "\n".join(result)


def _ensure_from_first(dockerfile: str) -> str:
    """Dockerfile의 첫 번째 라인이 FROM이어야 함"""
    lines = dockerfile.split("\n")

    # FROM 라인 찾기
    from_line_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("FROM ") or stripped.startswith("FROM\t"):
            from_line_idx = i
            break

    if from_line_idx is None:
        logger.warning("[Dockerfile] No FROM instruction found")
        return dockerfile

    if from_line_idx == 0:
        return dockerfile

    # FROM 이전의 빈 라인 제외한 모든 라인 제거
    logger.warning(f"[Dockerfile] Removed {from_line_idx} lines before FROM")
    return "\n".join(lines[from_line_idx:])


def _merge_env_layers(dockerfile: str) -> str:
    lines = dockerfile.split("\n")
    merged_lines: list[str] = []
    pending_envs: list[str] = []

    def flush_envs():
        if pending_envs:
            merged_lines.append(f"ENV {' '.join(pending_envs)}")
            pending_envs.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ENV "):
            env_val = stripped[4:].rstrip("\\").strip()
            if env_val:
                pending_envs.append(env_val)
        else:
            flush_envs()
            merged_lines.append(line)

    flush_envs()
    return "\n".join(merged_lines)


def _fix_wildcard_copy(dockerfile: str) -> str:
    lines = dockerfile.split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.search(r"COPY\s+--from=\S+\s+\S*\*\.\w+\s+\S+[^/]$", stripped):
            match = re.match(
                r"(\s*COPY\s+--from=\S+\s+)(\S+/\*\.\w+)(\s+)(\S+)",
                stripped,
            )
            if match:
                prefix, src, space, dest = match.groups()
                logger.warning(
                    f"[Dockerfile] Wildcard COPY to non-directory fixed: {stripped}"
                )
                result.append(f"{prefix}{src}{space}{dest}/")
                continue
        result.append(line)
    return "\n".join(result)


def _merge_run_layers(dockerfile: str) -> str:
    lines = dockerfile.split("\n")
    merged_lines: list[str] = []
    pending_commands: list[str] = []

    def flush_runs():
        if pending_commands:
            merged_lines.append("RUN " + " && \\\n    ".join(pending_commands))
            pending_commands.clear()

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("FROM "):
            flush_runs()
            pending_commands.clear()
            merged_lines.append(line)
        elif stripped.startswith("RUN "):
            cmd = stripped[4:]
            if cmd.startswith("#"):
                continue
            if cmd.endswith("\\"):
                cmd = cmd[:-1].rstrip()
            pending_commands.append(cmd)
        elif stripped and not stripped.startswith("#"):
            stripped_lc = stripped.lower()
            if "run " in stripped_lc and "\\" in stripped:
                merged_lines.append(
                    "# Skipping invalid line with backslash: " + stripped[:50]
                )
                continue
            flush_runs()
            merged_lines.append(line)
        else:
            flush_runs()
            merged_lines.append(line)

    flush_runs()
    return "\n".join(merged_lines)
