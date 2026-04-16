import json
import shlex


def _parse_copy_sources(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("COPY ") or "--from=" in stripped:
        return []

    copy_body = stripped[5:].strip()
    if copy_body.startswith("["):
        try:
            copy_parts = json.loads(copy_body)
        except json.JSONDecodeError:
            return []
        if isinstance(copy_parts, list) and len(copy_parts) >= 2:
            return [part for part in copy_parts[:-1] if isinstance(part, str)]
        return []

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return []

    if len(tokens) < 3 or tokens[0] != "COPY":
        return []

    source_start = 1
    while source_start < len(tokens) and tokens[source_start].startswith("--"):
        source_start += 1

    return tokens[source_start:-1]


def _parse_copy_instruction(line: str) -> tuple[list[str], str] | None:
    stripped = line.strip()
    if not stripped.startswith("COPY ") or "--from=" in stripped:
        return None

    copy_body = stripped[5:].strip()
    if copy_body.startswith("["):
        try:
            copy_parts = json.loads(copy_body)
        except json.JSONDecodeError:
            return None
        if isinstance(copy_parts, list) and len(copy_parts) >= 2:
            string_parts = [part for part in copy_parts if isinstance(part, str)]
            if len(string_parts) == len(copy_parts):
                return string_parts[:-1], string_parts[-1]
        return None

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return None

    if len(tokens) < 3 or tokens[0] != "COPY":
        return None

    source_start = 1
    while source_start < len(tokens) and tokens[source_start].startswith("--"):
        source_start += 1

    sources = tokens[source_start:-1]
    destination = tokens[-1]
    if not sources:
        return None

    return sources, destination
