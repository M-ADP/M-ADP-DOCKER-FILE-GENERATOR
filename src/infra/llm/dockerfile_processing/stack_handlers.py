import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fixers — (dockerfile: str) -> str
# ---------------------------------------------------------------------------

def _fix_alpine_adduser_home(dockerfile: str) -> str:
    """Alpine adduser -S 호출에 -H 추가 — 홈 디렉토리 생성을 막아 permission denied 방지."""
    def _add_no_home(m: re.Match) -> str:
        cmd = m.group(0)
        if "-H" in cmd:
            return cmd
        return re.sub(r"(-S\b)", r"\1 -H", cmd, count=1)

    return re.sub(r"adduser\s+[^&\n;|]*-S[^&\n;|]*", _add_no_home, dockerfile)


_ROOT_ONLY_RUN = re.compile(
    r"RUN\s+.*\b(chown|adduser|addgroup|useradd|groupadd|mkdir)\b",
    re.IGNORECASE,
)

def _move_user_to_end_of_stage(dockerfile: str) -> str:
    """USER <non-root>를 각 스테이지의 CMD/ENTRYPOINT 바로 앞으로 이동.

    root 명령 탐지 대신 USER 자체를 스테이지 끝으로 밀어서,
    모든 RUN이 root 권한으로 실행되도록 보장한다.
    """
    lines = dockerfile.split("\n")
    stage_starts = [i for i, line in enumerate(lines) if line.strip().startswith("FROM ")]
    if not stage_starts:
        return dockerfile
    stage_starts.append(len(lines))

    result: list[str] = []
    for start, end in zip(stage_starts, stage_starts[1:]):
        stage = lines[start:end]

        # 이 스테이지의 첫 번째 USER <non-root> 찾기
        user_local_idx: int | None = None
        user_line: str | None = None
        for i, line in enumerate(stage):
            stripped = line.strip()
            if re.match(r"USER\s+(?!root\b|0\b)\S", stripped):
                user_local_idx = i
                user_line = line
                break

        if user_line is None or user_local_idx is None:
            result.extend(stage)
            continue

        # USER 제거
        stage_no_user = [ln for i, ln in enumerate(stage) if i != user_local_idx]

        # CMD / ENTRYPOINT 앞에 삽입 (없으면 스테이지 끝에 추가)
        insert_at: int | None = None
        for i, line in enumerate(stage_no_user):
            stripped = line.strip()
            if re.match(r"(CMD|ENTRYPOINT)[\s\[]", stripped):
                insert_at = i
                break

        if insert_at is not None:
            stage_no_user.insert(insert_at, user_line)
        else:
            stage_no_user.append(user_line)

        logger.info(
            "[StackFixer] USER '%s' moved to idx %s in stage starting '%s'",
            user_line.strip(),
            insert_at if insert_at is not None else "end",
            stage[0].strip()[:60],
        )
        result.extend(stage_no_user)

    return "\n".join(result)


_NODE_INSTALL_PATTERN = re.compile(
    r"RUN\s+.*\b(?:pnpm|npm|yarn)\s+install\b", re.IGNORECASE
)
_COPY_SOURCE_PATTERN = re.compile(
    r"COPY\s+(?:--\S+\s+)*(?!\s*--from)(?:\.|package\.json|requirements\.txt|Pipfile)",
    re.IGNORECASE,
)


def _fix_node_install_before_copy(dockerfile: str) -> str:
    """npm/pnpm/yarn install이 COPY 전에 오는 경우 'COPY . .' 삽입.

    LLM이 패키지 install을 소스 복사 전에 실행하는 잘못된 순서 교정.
    ERR_PNPM_NO_PKG_MANIFEST / npm ERR! Cannot read properties of null 방지.
    """
    lines = dockerfile.split("\n")
    stage_starts = [i for i, l in enumerate(lines) if l.strip().startswith("FROM ")]
    if not stage_starts:
        return dockerfile
    stage_starts.append(len(lines))

    result: list[str] = []
    for start, end in zip(stage_starts, stage_starts[1:]):
        stage = lines[start:end]
        copy_seen = False
        new_stage: list[str] = []
        for line in stage:
            stripped = line.strip()
            if _COPY_SOURCE_PATTERN.match(stripped):
                copy_seen = True
            if _NODE_INSTALL_PATTERN.search(stripped) and not copy_seen:
                new_stage.append("COPY . .")
                copy_seen = True
                logger.info("[StackFixer] inserted 'COPY . .' before package install")
            new_stage.append(line)
        result.extend(new_stage)

    return "\n".join(result)


def _fix_yarn_frozen_lockfile(dockerfile: str) -> str:
    """yarn install --frozen-lockfile → yarn install --immutable 교정.

    Yarn Berry(.yarnrc.yml 기반)는 --frozen-lockfile 플래그를 지원하지 않으며
    --immutable을 사용해야 한다. Classic yarn(v1)은 --frozen-lockfile이 맞지만,
    LLM이 Berry 프로젝트에 잘못 적용하는 경우를 교정한다.
    """
    # .yarnrc.yml이 참조되는 Dockerfile에서만 교정
    if ".yarnrc.yml" not in dockerfile and "corepack" not in dockerfile.lower():
        return dockerfile
    return re.sub(
        r"yarn\s+install\s+--frozen-lockfile",
        "yarn install --immutable",
        dockerfile,
    )


def _fix_invalid_corepack(dockerfile: str) -> str:
    """잘못된 corepack 호출 패턴 교정.

    1. corepack prepare --<invalid-flag>  → corepack enable
    2. corepack enable /path/...          → corepack enable
       (경로를 packageManager 이름으로 오해하는 LLM 패턴)
    3. corepack enable && corepack enable → corepack enable (중복 제거)
    """
    # corepack prepare --<invalid> 교정
    dockerfile = re.sub(
        r"corepack\s+prepare\s+\S+\s+--(?!activate\b|json\b|output\b)\S*",
        "corepack enable",
        dockerfile,
    )
    # corepack enable /absolute/path → corepack enable
    dockerfile = re.sub(
        r"corepack\s+enable\s+/\S+",
        "corepack enable",
        dockerfile,
    )
    # corepack enable && corepack enable (중복) → corepack enable
    dockerfile = re.sub(
        r"corepack\s+enable(?:\s*&&\s*corepack\s+enable)+",
        "corepack enable",
        dockerfile,
    )
    return dockerfile


def _fix_nginx_cmd(dockerfile: str) -> str:
    dockerfile = re.sub(
        r'(CMD\s*\["nginx"[^\]]*?),?\s*"-c",\s*"[^"]*nginx\.conf"([^\]]*\])',
        r"\1\2",
        dockerfile,
    )
    dockerfile = re.sub(r',\s*(\])', r"\1", dockerfile)
    return dockerfile


def _fix_java_jdk_to_jre(dockerfile: str) -> str:
    """builder 이후 스테이지에서 eclipse-temurin:*-jdk → *-jre 교정."""
    parts = dockerfile.split("FROM ")
    if len(parts) < 3:
        return dockerfile
    fixed = ["FROM ".join(parts[:2])]  # builder까지는 그대로
    for part in parts[2:]:
        part = re.sub(
            r"(eclipse-temurin:\d+)-jdk(\S*)",
            r"\1-jre\2",
            part,
        )
        fixed.append(part)
    return "FROM ".join(fixed)


def _fix_node_static_runner(dockerfile: str) -> str:
    """alpine+apk add nginx 패턴을 node:22-alpine + serve 패턴으로 교정"""
    if "apk add" not in dockerfile and "apk --no-cache add" not in dockerfile:
        return dockerfile
    # FROM alpine:* 뒤에 apk add nginx가 오는 스테이지를 탐지
    if not re.search(r"FROM\s+alpine[:\s].*\n[\s\S]*?apk.*nginx", dockerfile):
        return dockerfile
    # FROM alpine:x.y → FROM node:22-alpine
    dockerfile = re.sub(
        r"(FROM\s+)alpine:\S+(\s+(?:AS\s+\S+)?)",
        r"\1node:22-alpine\2",
        dockerfile,
        count=1,
    )
    return dockerfile


def _fix_serve_not_installed(dockerfile: str) -> str:
    """CMD ["serve", ...] 사용 시 npm install -g serve가 없으면 추가.

    LLM이 serve CLI를 CMD에 쓰면서 설치 단계를 빠뜨리는 패턴 교정.
    """
    lines = dockerfile.split("\n")
    stage_starts = [i for i, l in enumerate(lines) if l.strip().startswith("FROM ")]
    if not stage_starts:
        return dockerfile
    stage_starts.append(len(lines))

    result: list[str] = []
    for start, end in zip(stage_starts, stage_starts[1:]):
        stage = lines[start:end]
        stage_text = "\n".join(stage)

        uses_serve_cmd = bool(re.search(r'CMD\s*\[.*"serve"', stage_text))
        has_serve_install = bool(re.search(r"npm\s+install\s+-g\s+serve|npx\s+serve", stage_text))

        if uses_serve_cmd and not has_serve_install:
            # CMD 라인 앞에 RUN npm install -g serve 삽입
            new_stage: list[str] = []
            for line in stage:
                stripped = line.strip()
                if re.match(r'CMD\s*\[.*"serve"', stripped):
                    new_stage.append("RUN npm install -g serve")
                    logger.info("[StackFixer] added 'npm install -g serve' before CMD serve")
                new_stage.append(line)
            result.extend(new_stage)
        else:
            result.extend(stage)

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Validators — (dockerfile: str) -> list[str]
# ---------------------------------------------------------------------------

def _validate_python_site_packages(dockerfile: str) -> list[str]:
    if dockerfile.count("FROM ") < 2:
        return []
    if not re.search(r"COPY\s+--from=\S+\s+/usr/local/lib/python", dockerfile):
        return [
            "Python 멀티스테이지 빌드: runner stage에 "
            "`COPY --from=builder /usr/local/lib/python3.x/site-packages` 누락. "
            "의존성이 런타임에 존재하지 않습니다."
        ]
    return []


def _validate_python_copy_before_install(dockerfile: str) -> list[str]:
    """requirements.txt/pyproject.toml이 pip install 전에 COPY되는지 확인합니다."""
    lines = [l.strip() for l in dockerfile.splitlines() if l.strip()]
    copy_seen = False
    for line in lines:
        if line.startswith("FROM "):
            copy_seen = False
            continue
        if re.match(r"COPY\b", line) and not re.search(r"--from=", line):
            src = line.split()[1] if len(line.split()) > 1 else ""
            if any(m in src for m in ("requirements", "pyproject", "setup.py", "Pipfile")):
                copy_seen = True
            if src in (".", ""):
                copy_seen = True
        if re.search(r"pip\s+install", line) and not copy_seen:
            return [
                "Python: pip install 전에 requirements.txt/pyproject.toml COPY가 없습니다. "
                "`COPY requirements.txt ./` 를 pip install RUN 명령 앞에 추가하세요."
            ]
    return []


def _validate_node_server_runtime(dockerfile: str) -> list[str]:
    """node-server runner에 node_modules가 있거나 npm install이 실행되는지 확인."""
    if dockerfile.count("FROM ") < 2:
        return []
    runner = "FROM ".join(dockerfile.split("FROM ")[2:])
    has_modules = "node_modules" in runner
    has_install = bool(re.search(r"\b(npm|yarn|pnpm|bun)\s+install\b", runner))
    if not has_modules and not has_install:
        return [
            "node-server: runner stage에 node_modules COPY 또는 npm install이 없습니다. "
            "`COPY --from=builder /app/node_modules ./node_modules` 를 추가하세요."
        ]
    return []


def _validate_java_runner_image(dockerfile: str) -> list[str]:
    """Java runner stage는 JDK가 아닌 JRE를 사용해야 합니다."""
    issues = []
    for stage in dockerfile.split("FROM ")[2:]:  # builder 이후 스테이지
        first_line = stage.splitlines()[0].lower()
        if "jdk" in first_line and "as builder" not in stage.lower().split("\n")[0]:
            issues.append(
                "Java: runner stage에 JDK 이미지가 감지되었습니다. "
                "`eclipse-temurin:17-jre` 를 사용하세요."
            )
    return issues


def _validate_java_jar_copy(dockerfile: str) -> list[str]:
    """JAR 파일이 runner stage로 COPY되는지 확인."""
    if dockerfile.count("FROM ") < 2:
        return []
    if not re.search(r"COPY\s+--from=\S+.*\.jar", dockerfile):
        return [
            "Java: runner stage에 JAR 파일 COPY가 없습니다. "
            "`COPY --from=builder /app.jar /app.jar` 를 추가하세요."
        ]
    return []


def _validate_go_multistage(dockerfile: str) -> list[str]:
    """Go는 멀티스테이지 빌드 필수, runner는 golang 이미지 사용 금지."""
    if dockerfile.count("FROM ") < 2:
        return ["Go: 멀티스테이지 빌드 필수. golang builder + alpine:3.19 runner를 사용하세요."]
    for stage in dockerfile.split("FROM ")[2:]:
        if re.match(r"golang:", stage.strip()):
            return [
                "Go: runner stage에 golang 이미지 사용이 감지되었습니다. "
                "`alpine:3.19` 또는 `scratch`를 사용하세요."
            ]
    if not re.search(r"COPY\s+--from=\S+", dockerfile):
        return ["Go: runner stage에 빌드 바이너리 COPY가 없습니다."]
    return []


def _validate_rust_multistage(dockerfile: str) -> list[str]:
    """Rust는 멀티스테이지 빌드 필수, runner는 rust 이미지 사용 금지."""
    if dockerfile.count("FROM ") < 2:
        return ["Rust: 멀티스테이지 빌드 필수. rust builder + debian:bookworm-slim runner를 사용하세요."]
    for stage in dockerfile.split("FROM ")[2:]:
        if re.match(r"rust:", stage.strip()):
            return [
                "Rust: runner stage에 rust 이미지 사용이 감지되었습니다. "
                "`debian:bookworm-slim` 또는 `alpine`을 사용하세요."
            ]
    if not re.search(r"COPY\s+--from=\S+", dockerfile):
        return ["Rust: runner stage에 빌드 바이너리 COPY가 없습니다."]
    return []


def _validate_dep_copy_before_install(
    install_pattern: str,
    dep_files: tuple[str, ...],
    label: str,
) -> Callable[[str], list[str]]:
    """의존성 파일 COPY가 install 명령 전에 오는지 검증하는 validator를 반환."""
    def _validator(dockerfile: str) -> list[str]:
        copy_seen = False
        for line in (l.strip() for l in dockerfile.splitlines() if l.strip()):
            if line.startswith("FROM "):
                copy_seen = False
                continue
            if re.match(r"COPY\b", line) and not re.search(r"--from=", line):
                parts = line.split()
                # --chown, --chmod 등 옵션 토큰 건너뛰고 실제 src 추출
                src_idx = 1
                while src_idx < len(parts) and parts[src_idx].startswith("--"):
                    src_idx += 1
                src = parts[src_idx] if src_idx < len(parts) else ""
                if any(dep in src for dep in dep_files) or src in (".", ""):
                    copy_seen = True
            if re.search(install_pattern, line) and not copy_seen:
                return [
                    f"{label}: install 명령 전에 의존성 파일 COPY가 없습니다. "
                    f"`COPY {dep_files[0]} ./` 를 install RUN 명령 앞에 추가하세요."
                ]
        return []
    return _validator


def _validate_user_before_root_cmds(dockerfile: str) -> list[str]:
    """USER <non-root> 이후에 root 전용 명령(chown/adduser/mkdir 등)이 오는지 감지.

    _move_user_to_end_of_stage 이후에도 남아있으면 LLM이 비정상 패턴을 생성한 것.
    """
    user_switched = False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("FROM "):
            user_switched = False
        if re.match(r"USER\s+(?!root\b|0\b)\S", stripped):
            user_switched = True
        if user_switched and _ROOT_ONLY_RUN.search(stripped):
            return [
                "permission denied 위험: USER <non-root> 이후에 chown/adduser/mkdir 등 "
                "root 전용 명령이 실행됩니다.\n"
                "올바른 Alpine 패턴:\n"
                "  RUN addgroup -S appgroup && adduser -S -H -G appgroup appuser "
                "&& mkdir -p /app && chown -R appuser:appgroup /app\n"
                "  CMD [\"entrypoint\"]\n"
                "  USER appuser  ← 반드시 CMD/ENTRYPOINT 바로 앞"
            ]
    return []


def _validate_corepack_usage(dockerfile: str) -> list[str]:
    """잘못된 corepack prepare --destination 패턴 감지."""
    if re.search(r"corepack\s+prepare.*--destination", dockerfile):
        return [
            "잘못된 corepack 패턴: `corepack prepare --destination`은 존재하지 않는 플래그입니다.\n"
            "올바른 pnpm 패턴: `corepack enable && pnpm install --frozen-lockfile`\n"
            "올바른 yarn berry 패턴: `corepack enable && yarn install --immutable`\n"
            "또는 npm 방식: `npm install -g pnpm && pnpm install --frozen-lockfile`"
        ]
    return []


def _validate_nginx_conf_ref(dockerfile: str) -> list[str]:
    m = re.search(r'CMD\s*\[.*?"nginx".*?"-c",\s*"([^"]+)"', dockerfile)
    if not m:
        return []
    conf_path = m.group(1)
    has_copy = bool(re.search(r"COPY\s+(?!--from)\S+\s+" + re.escape(conf_path), dockerfile))
    has_wildcard = bool(re.search(r"COPY\s+\.\s+\.", dockerfile))
    if not has_copy and not has_wildcard:
        return [
            f"nginx CMD가 '{conf_path}'를 참조하지만 해당 파일이 COPY되지 않습니다. "
            "nginx.conf를 COPY하거나 `-c` 플래그를 제거하세요."
        ]
    return []


def _validate_node_static_uses_serve(dockerfile: str) -> list[str]:
    """node-static/vite-static은 serve를 사용해야 함"""
    if re.search(r"\bnginx\b", dockerfile, re.IGNORECASE):
        return [
            "node-static/vite-static 스택에서 nginx 사용이 감지되었습니다. "
            "`node:22-alpine` + `serve -s dist` 패턴을 사용하세요."
        ]
    return []


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# 모든 스택에 무조건 적용 (stack 불문)
_UNIVERSAL_FIXERS: list[Callable[[str], str]] = [
    _fix_serve_not_installed,        # CMD ["serve",...] 있는데 npm install -g serve 없으면 추가 (USER 이동 전에 먼저)
    _fix_node_install_before_copy,   # npm/pnpm/yarn install 전에 COPY 없으면 'COPY . .' 삽입
    _move_user_to_end_of_stage,      # USER <non-root>를 스테이지 끝(CMD 앞)으로 이동 → 모든 RUN이 root 실행
    _fix_alpine_adduser_home,        # adduser -S에 -H 추가 → /home 생성 없이 유저 생성
    _fix_yarn_frozen_lockfile,       # Yarn Berry에서 --frozen-lockfile → --immutable 교정
    _fix_invalid_corepack,           # corepack prepare --destination 존재하지 않는 플래그 교정
    _fix_nginx_cmd,                  # nginx -c /missing.conf 패턴은 어느 스택에서도 잘못된 것
]

_UNIVERSAL_VALIDATORS: list[Callable[[str], list[str]]] = [
    _validate_user_before_root_cmds,  # USER 전환 후 root 전용 명령 → permission denied 방지
    _validate_corepack_usage,         # 잘못된 corepack 패턴 → 재시도 트리거
    _validate_nginx_conf_ref,         # nginx.conf 참조 누락은 스택 무관한 버그
]

_ruby_validator = _validate_dep_copy_before_install(
    r"bundle\s+install", ("Gemfile",), "Ruby"
)
_php_validator = _validate_dep_copy_before_install(
    r"composer\s+install", ("composer.json",), "PHP"
)
_gradle_validator = _validate_dep_copy_before_install(
    # gradle/gradlew + 빌드 태스크 (bootJar, bootWar, assemble, build 등)
    # 광범위한 gradle 단어 매칭 방지 — 반드시 빌드 태스크까지 포함
    r"(?:\.?/?gradlew|gradle)\s+(?:\S+\s+)*(?:build|bootJar|bootWar|assemble|jar|package|check)\b",
    ("build.gradle", "settings.gradle"),
    "Java Gradle",
)
_maven_validator = _validate_dep_copy_before_install(
    r"mvn\s+\S*(package|install|compile)", ("pom.xml",), "Java Maven"
)

def _fix_gradle_builder_image(dockerfile: str) -> str:
    """builder 스테이지의 FROM을 gradle 공식 이미지로 교체하고 ./gradlew → gradle 변환.

    gradle-wrapper.jar는 git에 커밋되지 않아 빌드 컨텍스트에 없는 경우가 많다.
    gradle:8-jdk17 이미지에는 gradle CLI가 내장되어 있어 wrapper 불필요.
    """
    # builder 스테이지 FROM 교체 (eclipse-temurin/openjdk/amazoncorretto JDK → gradle 이미지)
    dockerfile = re.sub(
        r"FROM\s+(?:eclipse-temurin|openjdk|amazoncorretto):\S*jdk\S*\s+(AS\s+builder\b)",
        r"FROM gradle:8-jdk17 \1",
        dockerfile,
        count=1,
        flags=re.IGNORECASE,
    )
    # ./gradlew → gradle (wrapper jar 불필요)
    dockerfile = re.sub(r"\./gradlew\b", "gradle", dockerfile)
    # 남은 단독 gradlew (경로 없이) → gradle
    dockerfile = re.sub(r"(?<![./\w])gradlew\b", "gradle", dockerfile)
    logger.info("[StackFixer] gradle: replaced ./gradlew → gradle, builder image → gradle:8-jdk17")
    return dockerfile


_GRADLE_BUILD_PATTERN = re.compile(
    r"(?:\.?/?gradlew|gradle)\s+(?:\S+\s+)*(?:build|bootJar|bootWar|assemble|jar|package|check)\b"
)


def _fix_gradle_copy_full_source(dockerfile: str) -> str:
    """Java-Gradle builder 스테이지에서 전체 소스 COPY를 보장.

    LLM이 build.gradle, gradlew, settings.gradle만 COPY하고 src/를 빠뜨리는 패턴 교정.
    gradle 빌드 명령 전에 COPY . . 또는 src/ COPY가 없으면 'COPY . .'를 삽입한다.
    """
    lines = dockerfile.split("\n")
    stage_starts = [i for i, l in enumerate(lines) if l.strip().startswith("FROM ")]
    if not stage_starts:
        return dockerfile
    stage_starts.append(len(lines))

    result: list[str] = []
    for start, end in zip(stage_starts, stage_starts[1:]):
        stage = lines[start:end]
        stage_text = "\n".join(stage)

        if "as builder" not in stage[0].strip().lower():
            result.extend(stage)
            continue

        if not _GRADLE_BUILD_PATTERN.search(stage_text):
            result.extend(stage)
            continue

        # 전체 소스 COPY 여부 확인 — "COPY . ." 또는 "COPY src/" 형태
        has_full_copy = bool(re.search(
            r"COPY\s+(?:--\S+\s+)*(?:\.\s+\.|src[/\s]|\.\/src)",
            stage_text,
        ))
        if has_full_copy:
            result.extend(stage)
            continue

        # gradle 빌드 RUN 라인 바로 앞에 COPY . . 삽입
        new_stage: list[str] = []
        inserted = False
        for line in stage:
            if not inserted and _GRADLE_BUILD_PATTERN.search(line.strip()):
                new_stage.append("COPY . .")
                inserted = True
                logger.info("[StackFixer] gradle: inserted 'COPY . .' before build command")
            new_stage.append(line)
        result.extend(new_stage)

    return "\n".join(result)


def _fix_java_jar_cmd_glob(dockerfile: str) -> str:
    """CMD/ENTRYPOINT의 JAR glob 패턴을 shell form으로 교체.

    exec form에서 glob은 셸이 확장하지 않아 실제 파일을 찾지 못함.
    CMD ["java", "-jar", "build/libs/*.jar"] → CMD ["sh","-c","java -jar build/libs/*.jar"]
    """
    def _replace_glob_cmd(m: re.Match) -> str:
        instruction = m.group(1)  # CMD or ENTRYPOINT
        jar_path = m.group(2)     # e.g. build/libs/*.jar
        return f'{instruction} ["sh", "-c", "java -jar {jar_path}"]'

    return re.sub(
        r'(CMD|ENTRYPOINT)\s*\[.*?"java".*?"-jar".*?"([^"]*\*[^"]*\.jar)".*?\]',
        _replace_glob_cmd,
        dockerfile,
    )


# 특정 스택에만 추가 적용
_STACK_FIXERS: dict[str, list[Callable[[str], str]]] = {
    "node-static":  [_fix_node_static_runner],
    "vite-static":  [_fix_node_static_runner],
    "astro":        [_fix_node_static_runner],
    "vue":          [_fix_node_static_runner],
    "svelte":       [_fix_node_static_runner],
    "java-gradle":  [_fix_java_jdk_to_jre, _fix_gradle_builder_image, _fix_gradle_copy_full_source, _fix_java_jar_cmd_glob],
    "java-maven":   [_fix_java_jdk_to_jre, _fix_java_jar_cmd_glob],
    "java":         [_fix_java_jdk_to_jre, _fix_java_jar_cmd_glob],  # prefix fallback
}

_STACK_VALIDATORS: dict[str, list[Callable[[str], list[str]]]] = {
    "python-fastapi": [_validate_python_site_packages, _validate_python_copy_before_install],
    "python-flask":   [_validate_python_site_packages, _validate_python_copy_before_install],
    "python":         [_validate_python_site_packages, _validate_python_copy_before_install],
    "node-static":    [_validate_node_static_uses_serve],
    "vite-static":    [_validate_node_static_uses_serve],
    "astro":          [_validate_node_static_uses_serve],
    "node-server":    [_validate_node_server_runtime],
    "java-gradle":    [_validate_java_runner_image, _validate_java_jar_copy, _gradle_validator],
    "java-maven":     [_validate_java_runner_image, _validate_java_jar_copy, _maven_validator],
    "java":           [_validate_java_runner_image, _validate_java_jar_copy],
    "go":             [_validate_go_multistage],
    "rust":           [_validate_rust_multistage],
    "ruby":           [_ruby_validator],
    "php":            [_php_validator],
}


def _resolve_stack_handlers(registry: dict, stack: str | None) -> list:
    """정확히 매칭되거나, prefix("python-fastapi" → "python") 매칭된 항목 반환."""
    if not stack:
        return []
    if stack in registry:
        return list(registry[stack])
    prefix = stack.split("-")[0]
    return list(registry.get(prefix, []))


_NEXTJS_STANDALONE_COPY = re.compile(
    r"COPY\s+--from=\S+\s+\S+/\.next/standalone\s+", re.IGNORECASE
)
# node server.js CMD (standalone 방식) — 경로 포함
_NEXTJS_SERVER_JS_CMD = re.compile(
    r'CMD\s*\["node",\s*"[^"]*server\.js"\]', re.IGNORECASE
)


def _fix_nextjs_standalone_without_config(
    dockerfile: str, store: dict[str, str]
) -> str:
    """standalone 방식 패턴이 있지만 next.config에 output: 'standalone'이 없으면 교정.

    두 가지 LLM 오류 패턴을 모두 처리:

    Pattern A — standalone COPY:
      COPY --from=builder /app/.next/standalone ./
      COPY --from=builder /app/.next/static ./.next/static
      CMD ["node", "server.js"]
      → COPY --from=builder /app ./ + CMD ["node_modules/.bin/next", "start"]

    Pattern B — 잘못된 .next destination + server.js CMD:
      COPY --from=builder /app/.next ./public/.next   ← 잘못된 dest
      CMD ["node", "public/.next/standalone/server.js"]
      → COPY dest를 ./.next로 교정 + 필요 COPY 추가 + CMD 교정
    """
    from pathlib import Path as _Path

    has_standalone_copy = bool(_NEXTJS_STANDALONE_COPY.search(dockerfile))
    has_server_js_cmd = bool(_NEXTJS_SERVER_JS_CMD.search(dockerfile))
    if not (has_standalone_copy or has_server_js_cmd):
        return dockerfile

    config_names = ("next.config.ts", "next.config.js", "next.config.mjs", "next.config.cjs")
    config_contents = [v for k, v in store.items() if _Path(k).name in config_names]
    if any("standalone" in c for c in config_contents):
        return dockerfile

    logger.warning(
        "[StackFixer] next.config에 output: 'standalone' 없는데 standalone 패턴 감지 — "
        "일반 .next 방식으로 교정"
    )

    lines = dockerfile.split("\n")
    result: list[str] = []
    app_copied = False          # /app 전체 COPY 여부
    node_modules_copied = False # node_modules COPY 여부
    pkg_json_copied = False     # package.json COPY 여부
    dotNext_correct = False     # .next → ./.next 정상 COPY 여부

    for line in lines:
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]

        # Pattern A: COPY --from=... /path/.next/standalone ./ → COPY --from=... /app ./
        m = re.match(r"(COPY\s+--from=\S+)\s+\S+/\.next/standalone\s+(\./?)\s*$", stripped, re.IGNORECASE)
        if m:
            result.append(f"{indent}{m.group(1)} /app ./")
            app_copied = True
            node_modules_copied = True
            pkg_json_copied = True
            dotNext_correct = True
            logger.info("[StackFixer] Pattern A: .next/standalone COPY → /app ./")
            continue

        # Pattern A: COPY --from=... /path/.next/static ... → 제거 (/app에 이미 포함)
        if re.match(r"COPY\s+--from=\S+\s+\S+/\.next/static\b", stripped, re.IGNORECASE) and app_copied:
            logger.info("[StackFixer] removed redundant .next/static COPY")
            continue

        # Pattern B: COPY --from=... /app/.next <wrong-dest> → COPY --from=... /app/.next ./.next
        m2 = re.match(r"(COPY\s+--from=\S+)\s+(\S+/\.next)\s+(\S+)\s*$", stripped, re.IGNORECASE)
        if m2:
            dest = m2.group(3)
            if dest not in ("./.next", ".next"):
                result.append(f"{indent}{m2.group(1)} {m2.group(2)} ./.next")
                dotNext_correct = True
                logger.info("[StackFixer] Pattern B: .next dest '%s' → ./.next", dest)
                continue
            else:
                dotNext_correct = True

        # node_modules COPY 여부 추적
        if re.match(r"COPY\s+--from=\S+\s+\S+/node_modules\b", stripped, re.IGNORECASE):
            node_modules_copied = True
        # package.json COPY 여부 추적
        if re.match(r"COPY\s+--from=\S+\s+\S+/package\.json\b", stripped, re.IGNORECASE):
            pkg_json_copied = True
        # /app 전체 COPY 여부 추적 (/app 또는 .../app 경로 모두 인식)
        if re.match(r"COPY\s+--from=\S+\s+(?:/app|\S+/app)\s+\./", stripped, re.IGNORECASE):
            app_copied = True
            node_modules_copied = True
            pkg_json_copied = True

        # CMD ["node", "...server.js"] → 필요 COPY 주입 후 CMD 교정
        if _NEXTJS_SERVER_JS_CMD.match(stripped):
            if not app_copied:
                if not node_modules_copied:
                    result.append(f"{indent}COPY --from=builder /app/node_modules ./node_modules")
                    logger.info("[StackFixer] injected COPY node_modules")
                if not pkg_json_copied:
                    result.append(f"{indent}COPY --from=builder /app/package.json .")
                    logger.info("[StackFixer] injected COPY package.json")
            result.append(f'{indent}CMD ["node_modules/.bin/next", "start"]')
            logger.info("[StackFixer] CMD node server.js → next start")
            continue

        result.append(line)

    return "\n".join(result)


def apply_stack_fixers(dockerfile: str, stack: str | None) -> str:
    for fixer in _UNIVERSAL_FIXERS:
        dockerfile = fixer(dockerfile)
    for fixer in _resolve_stack_handlers(_STACK_FIXERS, stack):
        dockerfile = fixer(dockerfile)
    return dockerfile


def apply_store_fixers(dockerfile: str, store: dict[str, str], stack: str | None) -> str:
    """store 내용을 참조해야 하는 fixers (next.config 등)."""
    dockerfile = _fix_nextjs_standalone_without_config(dockerfile, store)
    return dockerfile


def collect_stack_issues(dockerfile: str, stack: str | None) -> list[str]:
    issues: list[str] = []
    for validator in _UNIVERSAL_VALIDATORS:
        issues.extend(validator(dockerfile))
    for validator in _resolve_stack_handlers(_STACK_VALIDATORS, stack):
        issues.extend(validator(dockerfile))
    return issues
