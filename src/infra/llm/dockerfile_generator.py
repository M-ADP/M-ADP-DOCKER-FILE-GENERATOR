import json
import logging
import re
import shlex
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.core.generators import BaseDockerfileGenerator
from src.infra.llm.docker_hub_verifier import DockerHubVerifier
from src.infra.llm.nova import NovaLLM

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10
MAX_AGENT_RETRIES = 3

DOCKERIGNORE_COMMON = [
    ".git/",
    ".github/",
    ".gitignore",
    ".gitattributes",
    ".env",
    ".env.*",
    "*.log",
    "*.log.*",
    "README.md",
    "README.*",
    "CHANGELOG.md",
    "LICENSE",
    "Makefile",
    ".idea/",
    ".vscode/",
    ".sublime-project",
    ".sublime-workspace",
    ".DS_Store",
    "Thumbs.db",
    "*.swp",
    "*.swo",
    "*~",
    ".claude/",
    "*.md",
]

DOCKERIGNORE_NODE = [
    "node_modules/",
    "dist/",
    "build/",
    "out/",
    ".cache/",
    ".next/",
    ".nuxt/",
    "coverage/",
    ".nyc_output/",
    ".eslintcache",
    "*.tgz",
    "*.tar.gz",
    ".npm/",
    ".yarn/",
    ".yarn-integrity",
]

DOCKERIGNORE_PYTHON = [
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    "*.egg-info/",
    "*.egg",
    "dist/",
    "build/",
    ".venv/",
    "venv/",
    "env/",
    ".Python",
    "pip-log.txt",
    "pip-delete-this-directory.txt",
]

DOCKERIGNORE_JAVA_GRADLE = [
    ".gradle/",
    "build/",
    "*.class",
    "*.war",
    "*.ear",
]

DOCKERIGNORE_JAVA_MAVEN = [
    "target/",
    "*.class",
    "*.war",
    "*.ear",
    ".mvn/",
]

BUILD_REQUIRED_PATHS = {
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "uv.lock",
    "pom.xml",
    "mvnw",
    "mvnw.cmd",
    ".mvn/wrapper/maven-wrapper.jar",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradlew",
    "gradlew.bat",
    "gradle/wrapper/gradle-wrapper.jar",
    "go.mod",
    "go.sum",
}

OPTIONAL_NODE_COPY_FILES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
}


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


def _is_known_image(image: str) -> bool:
    for family_images in SUPPORTED_BASE_IMAGES.values():
        if image in family_images.values():
            return True
    return False


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
            pending_envs.append(stripped[4:])
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


def _validate_dockerfile_syntax(dockerfile: str) -> tuple[bool, list[str]]:
    """Dockerfile 문법 검증 - 문제 패턴 감지"""
    issues = []
    lines = dockerfile.split("\n")

    in_continuation = False
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

        if stripped.startswith("FROM ") and i > 1:
            prev_idx = i - 2
            while prev_idx >= 0 and not lines[prev_idx].strip():
                prev_idx -= 1
            if prev_idx >= 0:
                prev = lines[prev_idx].strip()
                if prev.endswith("\\"):
                    issues.append(
                        f"Line {i}: FROM appears after RUN line ending with backslash"
                    )

    return len(issues) == 0, issues


STACK_PATTERNS = {
    "nextjs": {
        "detector": ["next.config.js", "next.config.mjs", "next.config.ts"],
        "secondary": ["package.json"],
        "score": 100,
        "expose": "3000",
        "cmd": None,
    },
    "nuxt": {
        "detector": ["nuxt.config.js", "nuxt.config.ts"],
        "secondary": ["package.json"],
        "score": 100,
        "expose": "3000",
        "cmd": None,
    },
    "vue": {
        "detector": ["vue.config.js", "vue.config.ts"],
        "secondary": ["package.json"],
        "score": 90,
        "expose": "3000",
        "cmd": None,
    },
    "svelte": {
        "detector": ["svelte.config.js", "svelte.config.ts"],
        "secondary": ["package.json"],
        "score": 90,
        "expose": "3000",
        "cmd": None,
    },
    "astro": {
        "detector": ["astro.config.js", "astro.config.mjs", "astro.config.ts"],
        "secondary": ["package.json"],
        "score": 90,
        "expose": "3000",
        "cmd": None,
    },
    "vite-static": {
        "detector": ["vite.config.js", "vite.config.ts"],
        "secondary": ["package.json"],
        "score": 80,
        "expose": "3000",
        "cmd": "serve -s dist -l 3000",
    },
    "node-static": {
        "detector": [],
        "secondary": ["package.json"],
        "score": 70,
        "expose": "3000",
        "cmd": "serve -s dist -l 3000",
    },
    "node-server": {
        "detector": ["server.js", "app.js", "index.js"],
        "secondary": ["package.json"],
        "score": 60,
        "expose": "3000",
        "cmd": None,
    },
    "python-fastapi": {
        "detector": ["main.py"],
        "secondary": ["requirements.txt", "pyproject.toml"],
        "score": 80,
        "expose": "8000",
        "cmd": "uvicorn main:app --host 0.0.0.0 --port 8000",
    },
    "python-flask": {
        "detector": ["app.py"],
        "secondary": ["requirements.txt", "pyproject.toml"],
        "score": 70,
        "expose": "5000",
        "cmd": "flask run --host 0.0.0.0 --port 5000",
    },
    "java-gradle": {
        "detector": ["build.gradle", "build.gradle.kts"],
        "secondary": ["settings.gradle"],
        "score": 80,
        "expose": "8080",
        "cmd": None,
    },
    "java-maven": {
        "detector": ["pom.xml"],
        "secondary": [],
        "score": 80,
        "expose": "8080",
        "cmd": None,
    },
    "go": {
        "detector": ["go.mod"],
        "secondary": [],
        "score": 80,
        "expose": "8080",
        "cmd": None,
    },
    "rust": {
        "detector": ["Cargo.toml"],
        "secondary": [],
        "score": 80,
        "expose": "8080",
        "cmd": None,
    },
    "ruby": {
        "detector": ["Gemfile"],
        "secondary": [],
        "score": 80,
        "expose": "3000",
        "cmd": None,
    },
    "php": {
        "detector": ["composer.json"],
        "secondary": [],
        "score": 80,
        "expose": "8000",
        "cmd": None,
    },
}


def detect_stack(store: dict[str, str]) -> Optional[str]:
    """복수 파일 기반 점수 시스템으로 스택 감지"""
    files = set(Path(p).name for p in store.keys())

    scores: dict[str, int] = {}

    for stack_name, config in STACK_PATTERNS.items():
        score = 0

        # Primary detector files
        for detector in config["detector"]:
            if detector in files:
                score += config["score"]

        # Secondary confirmation files
        for secondary in config.get("secondary", []):
            if secondary in files:
                score += config["score"] // 4

        if score > 0:
            scores[stack_name] = score

    if not scores:
        return None

    # Return highest score stack
    return max(scores.items(), key=lambda x: x[1])[0]


SYSTEM_PROMPT = """당신은 Dockerfile 최적화 전문가입니다.
주어진 소스코드를 분석하여 최적화된 production-ready Dockerfile을 생성하세요.

## 핵심 최적화 원칙

### 1. 캐시 활용을 위한 명령어 순서
- **자주 변경되지 않는 명령어를 상단에 배치**
- 의존성 파일(package.json, requirements.txt 등)을 먼저 COPY 후 설치
- 소스 코드는 마지막에 COPY
- 이렇게 하면 소스 변경 시 의존성 설치 캐시를 재사용 가능

### 2. 멀티스테이지 빌드 필수 사용
- 빌드 스테이지와 런타임 스테이지 분리
- 빌드 도구, 컴파일러, devDependencies는 최종 이미지에서 제외
- `COPY --from=builder`로 빌드 결과만 복사

### 3. 레이어 수 최소화 (필수 준수 - 메모리 스파이크 방지)
- **모든 RUN 명령어는 `&&`로 결합하여 하나의 레이어로 생성**
- **install, build, cache clean을 하나의 RUN에 결합**
- **ENV 명령어도 하나로 결합**: `ENV VAR1=val1 VAR2=val2`
- **왜 필수인가**: 빌드 환경에서 Kaniko executor를 사용합니다. Kaniko는 RUN 명령이 끝날 때마다 전체 파일시스템 스냅샷을 뜹니다. `yarn install` 후 스냅샷 → 수만 개 파일 해싱으로 메모리 피크 발생 → `yarn build` 후 또 스냅샷 → 메모리 두 번 튐. 하나의 RUN으로 결합하면 중간 스냅샷을 생략하여 메모리 피크가 절반으로 줄어듭니다.

#### Node.js 정적 빌드 예시
```dockerfile
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund && npm run build && npm cache clean --force && rm -rf /root/.npm
COPY . .

FROM node:22-alpine
WORKDIR /app
COPY --from=builder /app/dist ./dist
RUN npm install -g serve && addgroup -S appgroup && adduser -S appuser -G appgroup
ENV NODE_ENV=production
USER appuser
EXPOSE 3000
CMD ["serve", "-s", "dist", "-l", "3000"]
```

#### Node.js 서버 예시
```dockerfile
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund && npm cache clean --force && rm -rf /root/.npm
COPY . .

FROM node:22-alpine
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/src ./src
RUN addgroup -S appgroup && adduser -S appuser -G appgroup && chown -R appuser:appgroup /app
ENV NODE_ENV=production
USER appuser
EXPOSE 3000
CMD ["node", "src/index.js"]
```

#### Python 예시
```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && rm -rf ~/.cache/pip
COPY . .

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /app ./
RUN groupadd -r appgroup && useradd -r -g appgroup appuser && \
    find . -type d -name '__pycache__' -exec rm -rf {{}} + && \
    find . -name '*.pyc' -delete && \
    chown -R appuser:appgroup /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 4. 이미지 크기 최소화
- Alpine 또는 Slim 베이스 이미지 사용 (node:22-alpine, python:3.12-slim 등)
- 패키지 설치 후 캐시/임시 파일 정리를 RUN 명령어 내에서 즉시 수행

#### Java Gradle 예시
```dockerfile
FROM eclipse-temurin:17-jdk AS builder
WORKDIR /app
COPY gradle/ gradle/
COPY gradlew build.gradle settings.gradle ./
RUN chmod +x gradlew && ./gradlew build --no-daemon
COPY src/ src/
RUN ./gradlew build --no-daemon && cp build/libs/*-SNAPSHOT.jar /app.jar

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /app.jar /app.jar
RUN groupadd -r appgroup && useradd -r -g appgroup appuser && chown -R appuser:appgroup /app
USER appuser
EXPOSE 8080
CMD ["java", "-jar", "/app.jar"]
```

#### Java Maven 예시
```dockerfile
FROM eclipse-temurin:17-jdk AS builder
WORKDIR /app
COPY pom.xml ./
RUN mvn dependency:go-offline
COPY src/ src/
RUN mvn clean package -DskipTests && cp target/*.jar /app.jar

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /app.jar /app.jar
RUN groupadd -r appgroup && useradd -r -g appgroup appuser && chown -R appuser:appgroup /app
USER appuser
EXPOSE 8080
CMD ["java", "-jar", "/app.jar"]
```

### 5. 베이스 이미지 검증 (필수)
- **openjdk 이미지 사용 금지** - Oracle 라이선스 정책 변경으로 Docker Hub에서 제거됨
- **베이스 이미지는 반드시 검증 필요** - `verify_docker_image` 도구를 사용하여 Docker Hub에서 존재 여부 확인
- **검증 방법**: FROM 이미지를 결정하기 전에 `verify_docker_image("image:tag")` 호출
  - 결과가 "EXISTS"면 사용 가능
  - 결과가 "NOT_FOUND"면 다른 태그나 이미지 검색
- **Java가 필요한 경우**: `eclipse-temurin:17-jdk` 또는 `eclipse-temurin:21-jdk` 사용 (openjdk 대체)
- **새로운 스택**: 검증된 이미지만 사용, 불확실하면 검색 도구 사용

## 스택 감지 및 템플릿 적용

소스코드에서 스택을 감지하고, 해당하는 검증된 템플릿을 사용하세요.
위 예시 템플릿을 참고하여 동일한 레이어 결합 패턴을 적용하세요.

### 지원 스택
{stack_info}

## 스택별 규칙

1. **Node.js 정적 빌드 (node-static)**
   - 빌드 스테이지: npm ci + npm run build + cache clean을 하나의 RUN에 결합
   - runner: serve 설치 + user 생성을 하나의 RUN에 결합
   - nginx 사용 금지

2. **Node.js 서버 (node-server)**
   - 빌드 스테이지: npm ci + cache clean을 하나의 RUN에 결합
   - runner: user 생성 + chown을 하나의 RUN에 결합
   - CMD는 package.json의 main 또는 scripts.start를 read_file로 확인

3. **Python (FastAPI, Flask)**
   - 빌드 스테이지: pip install + cache clean을 하나의 RUN에 결합
   - runner: user 생성 + pycache clean + chown을 하나의 RUN에 결합
   - ENV는 한 줄로 결합: `ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1`

4. **Java Gradle (java-gradle)**
   - **gradle-wrapper.jar는 빌드에 필수**: COPY 시 반드시 포함
   - 빌드 스테이지: `COPY gradle/ gradle/` 후 `COPY gradlew build.gradle ./` 후 `RUN ./gradlew build`
   - .dockerignore에서 `gradle/` 디렉토리 전체를 제외하면 gradle-wrapper.jar가 누락되어 빌드 실패
   - runner: JRE 베이스 + JAR 복사 + user 생성

5. **Java Maven (java-maven)**
   - 빌드 스테이지: `COPY pom.xml ./` 후 `COPY src/ src/` 후 `RUN mvn clean package`
   - runner: JRE 베이스 + JAR 복사 + user 생성

## 공통 필수 규칙

### Dockerfile과 .dockerignore 정합성
- Dockerfile에서 COPY하는 파일은 .dockerignore에서 제외되면 안 됩니다.
- package.json, package-lock.json, yarn.lock, pnpm-lock.yaml, bun.lockb 등 의존성 설치에 필요한 파일은 실제 존재하는 파일만 COPY하세요.
- npm/yarn/pnpm/bun lockfile을 한 줄에 모두 나열하지 마세요. 저장소에 있는 package manager의 lockfile만 선택하세요.

### 보안 설정
- WORKDIR /app 고정
- EXPOSE 포트 명시
- COPY 명령어는 소스와 목적지 사이에 공백 포함
- 비루트 사용자 필수 설정

### 네트워크 안정성
- `npm ci --fetch-retries=5 --fetch-retry-mintimeout=20000`
- `yarn install --network-timeout 100000`

## 출력 형식
- 응답은 반드시 FROM 명령어로 시작
- 마크다운 코드 블록(```) 사용 금지
- # 로 시작하는 주석 포함 금지
- 설명 없이 순수한 Dockerfile 명령어만 출력
- 각 스테이지는 `AS <name>`으로 명명

## 절대 금지 사항
- openjdk:* 이미지 사용 금지 → eclipse-temurin 사용
- 분리된 RUN 명령어 금지 → 반드시 &&로 결합
- 분리된 ENV 명령어 금지 → 반드시 한 줄로 결합
- COPY . . 를 의존성 설치 전에 배치 금지
- 루트 유저로 실행 금지 → 반드시 비루트 사용자 설정
- **Java JAR 와일드카드 금지**: `COPY --from=builder /app/build/libs/*.jar /app.jar` 금지
  - Gradle/Maven 빌드는 *.jar로 여러 JAR 생성 (예: app.jar + app-plain.jar)
  - 와일드카드를 단일 파일에 복사하면 Docker/Kaniko 에러 발생
  - 빌드 단계에서 `cp build/libs/*-SNAPSHOT.jar /app.jar`로 단일 파일 복사 후
    `COPY --from=builder /app.jar /app.jar` 사용

## 스택 감지 결과 검증 (필수)

**주의: 시스템이 감지한 스택 정보는 참고용입니다. 반드시 직접 파일을 읽어 검증하세요.**

1. 감지된 스택이 표시되더라도, 반드시 `read_file`로 패키지 매니저 파일(package.json, build.gradle 등)을 읽어 확인
2. package.json의 dependencies를 확인하여 프레임워크(next, react, vue, express 등) 식별
3. 감지된 스택과 실제 파일 내용이 다르면 **실제 파일 내용을 우선**
4. 특히 주의:
   - Next.js 프로젝트: package.json에 "next" 의존성 + next.config.* 파일 존재
   - Spring Boot 프로젝트: build.gradle에 "spring-boot" 플러그인 + src/main/java 디렉토리
   - Flask/FastAPI: requirements.txt에 flask/fastapi 포함
   - 반드시 read_file로 확인 후 판단

## 알 수 없는 스택 처리 (필수)

감지된 스택이 없거나 생소한 스택인 경우, 다음 프로세스를 반드시 따르세요:

### 1단계: 패키지 매니저/빌드 파일 분석
read_file로 다음 파일들을 순서대로 확인:
- **패키지 매니저**: package.json, requirements.txt, Cargo.toml, Gemfile, pom.xml, build.gradle, go.mod, pyproject.toml, composer.json, mix.exs, pubspec.yaml, Package.swift, *.csproj, CMakeLists.txt
- **진입점**: main.py, main.go, main.rs, Main.cs, index.js, app.rb, index.php, server.js, app.js, server.py, lib/main.ex, web/main.go
- 분석 결과로 어떤 언어/프레임워크인지 추론

### 2단계: 베이스 이미지 검색 및 검증
- `search_docker_image` 도구로 해당 언어의 권장 이미지를 검색
- 검색 결과를 받은 후 `verify_docker_image`로 존재 여부 최종 검증
- EXISTS 확인 후 FROM에 사용

### 3단계: Dockerfile 구조 결정
언어별 기본 구조:

| 언어 | 베이스 이미지 | 의존성 설치 | 빌드 | 실행 |
|------|-------------|------------|------|------|
| Rust | rust:1.75-slim | cargo build --release | 멀티스테이지 필수 | COPY binary → alpine/debian |
| Ruby | ruby:3.3-slim | bundle install | 보통 불필요 | ruby app.rb |
| PHP | php:8.2-fpm | composer install | 보통 불필요 | php-fpm 또는 artisan serve |
| Elixir | elixir:1.16-otp-26 | mix deps.get + mix compile | 멀티스테이지 권장 | mix phx.server |
| .NET | mcr.microsoft.com/dotnet/sdk:8.0 | dotnet restore | dotnet publish | COPY publish → runtime 이미지 |
| Swift | swift:5.9 | swift build | 멀티스테이지 필수 | COPY binary → slim |
| Dart | dart:3.2 | dart pub get | dart compile exe | COPY binary → slim |
| C/C++ | gcc:13 | make | 멀티스테이지 필수 | COPY binary → debian-slim |
| Haskell | haskell:9.6 | cabal build | 멀티스테이지 필수 | COPY binary → debian-slim |
| Zig | zig:0.12 | zig build | 멀티스테이지 권장 | COPY binary → alpine |
| Go | golang:1.22-alpine | go mod download | 멀티스테이지 필수 | COPY binary → alpine |

### 4단계: 공통 규칙 준수
- 멀티스테이지 빌드: 컴파일/빌드가 필요한 언어는 반드시 적용
- 레이어 결합: RUN 명령어는 &&로 결합
- 비루트 사용자 설정 필수
- EXPOSE 포트 명시 (기본 8080, 프레임워크별 다를 수 있음)
- ENTRYPOINT 또는 CMD로 실행 명령 지정"""

HUMAN_PROMPT = """다음은 소스코드 디렉토리 구조입니다.

{tree}

{context}

{detect_info}

**도구 사용 순서**:
1. read_file 도구로 Dockerfile 작성에 필요한 추가 파일을 읽으세요
2. **(스택을 알 수 없을 때)**: search_docker_image 언어명)으로 권장 이미지 검색
3. verify_docker_image 도구로 베이스 이미지가 Docker Hub에 존재하는지 검증하세요
   - 예: verify_docker_image("node:22-alpine"), verify_docker_image("python:3.12-slim")
   - EXISTS 결과를 받으면 해당 이미지를 FROM에 사용
   - NOT_FOUND 결과를 받으면 다른 태그나 이미지를 검색
4. 검증된 베이스 이미지로 Dockerfile을 생성하세요"""


class DockerfileGenerator(BaseDockerfileGenerator):
    def __init__(self) -> None:
        self.llm = NovaLLM()
        self.stack_patterns = STACK_PATTERNS

    def _build_stack_info(self) -> str:
        lines = []
        for name, config in self.stack_patterns.items():
            detectors = config.get("detector", [])
            secondaries = config.get("secondary", [])
            if detectors:
                lines.append(f"- {name}: {detectors} (score: {config.get('score', 0)})")
            elif secondaries:
                lines.append(
                    f"- {name}: {secondaries} (fallback, score: {config.get('score', 0)})"
                )
        return "\n".join(lines)

    def _build_detect_info(self, stack: Optional[str]) -> str:
        if stack is None:
            return (
                "감지된 스택 없음. 파일을 직접 분석하여 적절한 Dockerfile을 생성하세요."
            )

        config = self.stack_patterns[stack]
        lines = [f"감지된 스택: {stack}", f"- EXPOSE: {config['expose']}"]

        if stack == "nextjs":
            lines += [
                "- CMD: `npm start` 또는 `yarn start` (package.json scripts 확인)",
                "- **중요**: Next.js는 standalone 빌드가 필요",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                "2. **package.json COPY**: `COPY package.json <실제로 존재하는 lockfile> ./`",
                "   - package-lock.json이 있으면 npm ci 사용: `COPY package.json package-lock.json ./`",
                "   - yarn.lock이 있으면 yarn 사용: `COPY package.json yarn.lock ./`",
                "   - pnpm-lock.yaml이 있으면 pnpm 사용: `COPY package.json pnpm-lock.yaml ./`",
                "   - 존재하지 않는 lockfile을 COPY에 포함하지 마세요.",
                "3. **레이어 결합 필수**: package manager에 맞춰 install/build/cache clean을 한 RUN에 결합",
                "   - npm: `RUN npm ci && npm run build && npm cache clean --force && rm -rf /root/.npm`",
                "   - yarn: `RUN yarn install --frozen-lockfile && yarn build && yarn cache clean`",
                "4. **런타임 스테이지**: `FROM node:22-alpine`",
                "5. **복사**: `COPY --from=builder /app/.next ./.next`",
                "   `COPY --from=builder /app/public ./public`",
                "   `COPY --from=builder /app/node_modules ./node_modules`",
                "   `COPY --from=builder /app/package.json ./package.json`",
                "6. **레이어 결합 필수**: `RUN addgroup -S appgroup && adduser -S appuser -G appgroup && chown -R appuser:appgroup /app`",
                "7. **환경변수 결합**: `ENV NODE_ENV=production`",
                "8. **비루트 사용자**: `USER appuser`",
                "9. **CMD**: `CMD ['npm', 'start']` 또는 `CMD ['yarn', 'start']`",
            ]
        elif stack == "nuxt":
            lines += [
                "- CMD: `npm run start` 또는 `yarn start`",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                "2. **레이어 결합 필수**: `RUN npm ci && npm run build && npm cache clean --force && rm -rf /root/.npm`",
                "3. **런타임 스테이지**: `FROM node:22-alpine`",
                "4. **복사**: `COPY --from=builder /app/.output ./.output`",
                "5. **CMD**: `CMD ['node', '.output/server/index.mjs']`",
            ]
        elif stack in ("vue", "svelte", "vite-static"):
            lines += [
                f"- CMD: {config.get('cmd', 'serve -s dist -l 3000')}",
                "- 빌드: `npm run build` → dist/ 생성",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                "2. **레이어 결합 필수**: `RUN npm ci && npm run build && npm cache clean --force && rm -rf /root/.npm`",
                "3. **런타임 스테이지**: `FROM node:22-alpine`",
                "4. **복사**: `COPY --from=builder /app/dist ./dist`",
                "5. **serve 설치 + user**: `RUN npm install -g serve && addgroup -S appgroup && adduser -S appuser -G appgroup`",
                "6. **CMD**: `CMD ['serve', '-s', 'dist', '-l', '3000']`",
            ]
        elif stack == "astro":
            lines += [
                "- 빌드: `npm run build` → dist/ 생성",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                "2. **레이어 결합 필수**: `RUN npm ci && npm run build && npm cache clean --force && rm -rf /root/.npm`",
                "3. **런타임 스테이지**: `FROM node:22-alpine`",
                "4. **복사**: `COPY --from=builder /app/dist ./dist`",
                "5. **serve 설치**: `RUN npm install -g serve`",
                "6. **CMD**: `CMD ['serve', '-s', 'dist', '-l', '3000']`",
            ]
        elif stack == "node-static":
            lines += [
                f"- CMD: {config['cmd']}",
                "- 빌드: `npm run build` (또는 yarn build) → dist/ 생성",
                "- runner: node:22-alpine + serve",
                "- runner에서 npm ci --production 불필요 (정적 파일만 필요)",
                "- nginx 사용 금지 (외부 설정 파일 의존성 위험)",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                "2. **레이어 결합 필수**: package.json 먼저 COPY 후 `RUN npm ci --no-audit --no-fund && npm run build && npm cache clean --force && rm -rf /root/.npm`",
                "3. **런타임 스테이지**: `FROM node:22-alpine`",
                "4. **빌드 결과만 복사**: `COPY --from=builder /app/dist ./dist`",
                "5. **레이어 결합 필수**: `RUN npm install -g serve && addgroup -S appgroup && adduser -S appuser -G appgroup`",
                "6. **환경변수 결합**: `ENV NODE_ENV=production`",
                "7. **비루트 사용자**: `USER appuser`",
            ]
        elif stack == "node-server":
            lines += [
                "- CMD: package.json의 main 또는 scripts.start를 read_file로 확인 후 결정",
                "- runner에서 npm ci --production (또는 yarn install --production) 필요",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                "2. **레이어 결합 필수**: `RUN npm ci --no-audit --no-fund && npm cache clean --force && rm -rf /root/.npm`",
                "3. **런타임 스테이지**: `FROM node:22-alpine`",
                "4. **production 의존성만**: `COPY --from=builder /app/node_modules ./node_modules`",
                "5. **소스 코드 복사**: `COPY --from=builder /app/src ./src` (또는 필요한 파일만)",
                "6. **레이어 결합 필수**: `RUN addgroup -S appgroup && adduser -S appuser -G appgroup && chown -R appuser:appgroup /app`",
                "7. **환경변수 결합**: `ENV NODE_ENV=production`",
                "8. **비루트 사용자**: `USER appuser`",
            ]
        elif stack and stack.startswith("python"):
            lines += [
                f"- CMD: {config['cmd']}",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM python:3.12-slim AS builder`",
                "2. **레이어 결합 필수**: `RUN pip install --no-cache-dir -r requirements.txt && rm -rf ~/.cache/pip`",
                "3. **런타임 스테이지**: `FROM python:3.12-slim`",
                "4. **의존성 복사**: `COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages`",
                "5. **환경변수 결합 필수**: `ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1`",
                "6. **레이어 결합 필수**: `RUN groupadd -r appgroup && useradd -r -g appgroup appuser && find . -type d -name '__pycache__' -exec rm -rf {{}} + && find . -name '*.pyc' -delete && chown -R appuser:appgroup /app`",
                "7. **비루트 사용자**: `USER appuser`",
            ]
        elif stack and stack.startswith("java"):
            lines += [
                "- CMD: 빌드 결과 JAR 파일 실행 (read_file로 build.gradle/pom.xml 확인)",
                "- **베이스 이미지 필수**: `FROM eclipse-temurin:17-jdk AS builder` (openjdk 사용 금지!)",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM eclipse-temurin:17-jdk AS builder`",
                "2. **레이어 결합 필수**: `RUN ./gradlew build --no-daemon && cp build/libs/*-SNAPSHOT.jar /app.jar` (Gradle)",
                "   또는 `RUN mvn clean package -DskipTests && cp target/*.jar /app.jar` (Maven)",
                "3. **런타임 스테이지**: `FROM eclipse-temurin:17-jre` (JRE만 필요)",
                "4. **JAR 복사**: `COPY --from=builder /app.jar /app.jar`",
                "5. **레이어 결합 필수**: `RUN groupadd -r appgroup && useradd -r -g appgroup appuser && chown -R appuser:appgroup /app`",
                "6. **비루트 사용자**: `USER appuser`",
                "7. **CMD**: `CMD ['java', '-jar', '/app.jar']`",
                "",
                "**절대 주의: JAR 파일 복사 방식**",
                "- Gradle 빌드는 -plain.jar와 실행 가능한 JAR 2개를 생성합니다.",
                "- `COPY --from=builder /app/build/libs/*.jar /app.jar` 사용 금지!",
                "- 와일드카드(*.jar)로 여러 파일을 단일 파일명(/app.jar)에 복사하면 Docker/Kaniko 에러 발생.",
                "- 빌드 스테이지에서 `cp`로 단일 파일로 먼저 복사한 후, `COPY --from=builder /app.jar /app.jar` 사용.",
                "",
                "**중요: .dockerignore 규칙**",
                "- gradle/wrapper/gradle-wrapper.jar 파일은 빌드에 필수입니다.",
                "- .dockerignore에서 `gradle/` 디렉토리 전체를 제외하면 안 됩니다.",
                "- build.gradle, settings.gradle, gradlew, gradlew.bat는 제외하지 않습니다.",
                "- .gradle/, build/, *.class, *.jar(빌드 결과)만 제외합니다.",
            ]
        elif stack == "go":
            lines += [
                "- CMD: Go 바이너리 실행",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM golang:1.22-alpine AS builder`",
                "2. **레이어 결합 필수**: `RUN apk add --no-cache git && go mod download && CGO_ENABLED=0 go build -o /app/main .`",
                "3. **런타임 스테이지**: `FROM alpine:3.19`",
                "4. **바이너리 복사**: `COPY --from=builder /app/main /app/main`",
                "5. **레이어 결합 필수**: `RUN addgroup -S appgroup && adduser -S appuser -G appgroup && chown -R appuser:appgroup /app`",
                "6. **비루트 사용자**: `USER appuser`",
                "7. **CMD**: `CMD ['/app/main']`",
            ]
        elif config["cmd"]:
            lines.append(f"- CMD: {config['cmd']}")

        lines.append("\n위 규칙을 반드시 준수하세요.")
        return "\n".join(lines)

    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        context: str,
    ) -> tuple[str, str, int]:
        stack = detect_stack(store)
        logger.info(f"[DockerfileGenerator] detected stack: {stack}")

        dockerignore = generate_dockerignore(store, stack)
        verifier = DockerHubVerifier()

        # Pydantic schemas for structured tools
        class ReadFileInput(BaseModel):
            path: str = Field(
                description="소스코드 파일의 경로. 트리에 표시된 경로를 그대로 사용하세요."
            )

        class VerifyDockerImageInput(BaseModel):
            image_with_tag: str = Field(
                description="Docker Hub에서 검증할 이미지 태그. 'image:tag' 형식 (예: 'node:22-alpine', 'python:3.12-slim')"
            )

        class SearchDockerImageInput(BaseModel):
            language: str = Field(
                description="검색할 프로그래밍 언어 이름 (예: 'rust', 'ruby', 'php', 'elixir', 'swift', 'dart', 'kotlin')"
            )

        @tool(args_schema=ReadFileInput)
        def read_file(path: str) -> str:
            """소스코드 파일의 내용을 읽습니다."""
            content = store.get(path)
            if content is None:
                return f"[오류] 파일을 찾을 수 없습니다: {path}"
            logger.info(f"[DockerfileGenerator] read_file: {path}")
            return content

        @tool(args_schema=VerifyDockerImageInput)
        async def verify_docker_image(image_with_tag: str) -> str:
            """Docker Hub에서 베이스 이미지 태그가 존재하는지 검증합니다."""
            parts = image_with_tag.split(":")
            if len(parts) != 2:
                return (
                    f"ERROR: Invalid format. Use 'image:tag' (e.g., 'node:22-alpine')"
                )

            image, tag = parts
            result = await verifier.verify(image, tag)

            if result["exists"]:
                return f"EXISTS: {image_with_tag} - verified, size: {result.get('size', 'unknown')} bytes"
            else:
                error = result.get("error", "unknown")
                suggestion = result.get("suggestion", "")
                return f"NOT_FOUND: {image_with_tag} - {error}. {suggestion}"

        @tool(args_schema=SearchDockerImageInput)
        def search_docker_image(language: str) -> str:
            """프로그래밍 언어에 대한 권장 베이스 이미지를 검색합니다."""
            suggested = verifier.suggest_image(language)
            if suggested:
                return f"SUGGESTED: {suggested} - 검색된 언어: {language}. 이제 verify_docker_image('{suggested}')로 검증하세요."
            else:
                detected = verifier.detect_language_from_files(set(store.keys()))
                if detected:
                    alt_suggested = verifier.suggest_image(detected)
                    if alt_suggested:
                        return f"SUGGESTED: {alt_suggested} - 파일 분석으로 언어 감지: {detected}. verify_docker_image('{alt_suggested}')로 검증하세요."
                return f"NOT_FOUND: {language} - 지원하지 않는 언어입니다. 지원 언어: python, node, java, go, rust, ruby, php, elixir, dotnet, swift, dart, kotlin, scala, clojure, perl, lua, haskell, c, c++, zig, nim, crystal, deno, bun, r, julia"

        llm_with_tools = self.llm.client.bind_tools(
            [read_file, verify_docker_image, search_docker_image]
        )
        messages = [
            SystemMessage(
                content=SYSTEM_PROMPT.format(stack_info=self._build_stack_info())
            ),
            HumanMessage(
                content=HUMAN_PROMPT.format(
                    tree=tree,
                    context=context,
                    detect_info=self._build_detect_info(stack),
                )
            ),
        ]

        dockerfile = await self._run_agent_with_retry(
            llm_with_tools,
            read_file,
            verify_docker_image,
            search_docker_image,
            messages,
        )
        dockerfile = _remove_missing_optional_copy_sources(dockerfile, store)
        dockerignore = _reconcile_dockerignore_with_dockerfile(
            dockerignore,
            dockerfile,
        )
        port = self._extract_port(dockerfile, stack)
        return dockerfile, dockerignore, port

    @staticmethod
    def _extract_port(dockerfile: str, stack: Optional[str]) -> int:
        match = re.search(r"EXPOSE\s+(\d+)", dockerfile)
        if match:
            return int(match.group(1))

        if stack and stack in STACK_PATTERNS:
            return int(STACK_PATTERNS[stack]["expose"])

        return 8080

    async def _run_agent_with_retry(
        self,
        llm_with_tools,
        read_file_tool: Callable,
        verify_image_tool: Callable,
        search_image_tool: Callable,
        messages: list,
    ) -> str:
        last_error = None

        for attempt in range(MAX_AGENT_RETRIES):
            try:
                result = await self._run_agent(
                    llm_with_tools,
                    read_file_tool,
                    verify_image_tool,
                    search_image_tool,
                    messages,
                )
                return result
            except ValueError as e:
                last_error = e
                logger.warning(
                    f"[DockerfileGenerator] Attempt {attempt + 1}/{MAX_AGENT_RETRIES} failed: {e}"
                )
                continue
            except Exception as e:
                last_error = e
                logger.error(
                    f"[DockerfileGenerator] Attempt {attempt + 1}/{MAX_AGENT_RETRIES} error: {e}"
                )
                continue

        raise ValueError(
            f"Failed to generate Dockerfile after {MAX_AGENT_RETRIES} attempts. Last error: {last_error}"
        )

    async def _run_agent(
        self,
        llm_with_tools,
        read_file_tool: Callable,
        verify_image_tool: Callable,
        search_image_tool: Callable,
        messages: list,
    ) -> str:
        response = None

        for iteration in range(MAX_TOOL_ITERATIONS):
            response = await llm_with_tools.ainvoke(messages)
            logger.info(
                f"[DockerfileGenerator] iteration={iteration + 1}, "
                f"tool_calls={len(response.tool_calls)}"
            )

            if not response.tool_calls:
                break

            messages.append(response)
            for tc in response.tool_calls:
                tool_name = tc.get("name", "")
                tool_args = tc.get("args", {})

                logger.info(
                    f"[DockerfileGenerator] tool_call: {tool_name}({tool_args})"
                )

                try:
                    if tool_name == "read_file":
                        # Fallback: if image_with_tag is passed instead of path
                        if "image_with_tag" in tool_args and "path" not in tool_args:
                            logger.warning(
                                f"[DockerfileGenerator] LLM confused parameters, "
                                f"image_with_tag={tool_args['image_with_tag']} passed to read_file"
                            )
                            # Try to use it as path if it looks like a file path
                            path_value = tool_args["image_with_tag"]
                            if "/" in path_value or "." in path_value:
                                result = read_file_tool.invoke({"path": path_value})
                            else:
                                result = f"[오류] read_file에는 파일 경로가 필요합니다. 받은 값: {path_value}"
                        else:
                            result = read_file_tool.invoke(tool_args)
                    elif tool_name == "verify_docker_image":
                        result = await verify_image_tool.invoke(tool_args)
                    elif tool_name == "search_docker_image":
                        result = search_image_tool.invoke(tool_args)
                    else:
                        result = f"[오류] 알 수 없는 도구: {tool_name}"

                except Exception as e:
                    logger.error(f"[DockerfileGenerator] tool error: {e}")
                    result = f"[오류] 도구 실행 실패: {e}"

                messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))

        if response is None or not response.content:
            raise ValueError("LLM returned empty response")

        content = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )
        return self._clean(content)

    @staticmethod
    def _clean(content: str) -> str:
        if not content or not content.strip():
            raise ValueError("LLM returned empty content before cleaning")

        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
        content = re.sub(r"```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"^\s*#.*\n?", "", content, flags=re.MULTILINE)
        content = re.sub(
            r"(COPY\s+\S+)\.([ \t]*/|[ \t]*$)", r"\1 .\2", content, flags=re.MULTILINE
        )
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = _normalize_continuation_lines(content)
        content = _fix_wildcard_copy(content)
        content = _merge_run_layers(content)
        content = _merge_env_layers(content)
        content = _sanitize_base_images(content)
        content = _remove_invalid_lines(content)
        content = _ensure_from_first(content)

        result = content.strip()
        if not result:
            raise ValueError("Dockerfile content became empty after cleaning")
        if not result.startswith("FROM"):
            raise ValueError(
                f"Dockerfile must start with FROM instruction. Got: {result[:100]}"
            )

        # Dockerfile 문법 검증
        is_valid, issues = _validate_dockerfile_syntax(result)
        if not is_valid:
            for issue in issues:
                logger.error(f"[Dockerfile] Syntax issue: {issue}")
            raise ValueError(f"Dockerfile has syntax errors: {'; '.join(issues[:3])}")

        return result
