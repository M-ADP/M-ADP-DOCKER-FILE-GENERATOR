import re
from typing import Callable

# ---------------------------------------------------------------------------
# Fixers — (dockerfile: str) -> str
# ---------------------------------------------------------------------------

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
                src = line.split()[1] if len(line.split()) > 1 else ""
                if any(dep in src for dep in dep_files) or src in (".", ""):
                    copy_seen = True
            if re.search(install_pattern, line) and not copy_seen:
                return [
                    f"{label}: install 명령 전에 의존성 파일 COPY가 없습니다. "
                    f"`COPY {dep_files[0]} ./` 를 install RUN 명령 앞에 추가하세요."
                ]
        return []
    return _validator


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
    _fix_nginx_cmd,  # nginx -c /missing.conf 패턴은 어느 스택에서도 잘못된 것
]

_UNIVERSAL_VALIDATORS: list[Callable[[str], list[str]]] = [
    _validate_nginx_conf_ref,  # nginx.conf 참조 누락은 스택 무관한 버그
]

_ruby_validator = _validate_dep_copy_before_install(
    r"bundle\s+install", ("Gemfile",), "Ruby"
)
_php_validator = _validate_dep_copy_before_install(
    r"composer\s+install", ("composer.json",), "PHP"
)
_gradle_validator = _validate_dep_copy_before_install(
    r"gradlew?\s+\S*build|gradle\s+\S*build", ("build.gradle", "settings.gradle"), "Java Gradle"
)
_maven_validator = _validate_dep_copy_before_install(
    r"mvn\s+\S*(package|install|compile)", ("pom.xml",), "Java Maven"
)

# 특정 스택에만 추가 적용
_STACK_FIXERS: dict[str, list[Callable[[str], str]]] = {
    "node-static":  [_fix_node_static_runner],
    "vite-static":  [_fix_node_static_runner],
    "astro":        [_fix_node_static_runner],
    "vue":          [_fix_node_static_runner],
    "svelte":       [_fix_node_static_runner],
    "java-gradle":  [_fix_java_jdk_to_jre],
    "java-maven":   [_fix_java_jdk_to_jre],
    "java":         [_fix_java_jdk_to_jre],  # prefix fallback
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


def apply_stack_fixers(dockerfile: str, stack: str | None) -> str:
    for fixer in _UNIVERSAL_FIXERS:
        dockerfile = fixer(dockerfile)
    for fixer in _resolve_stack_handlers(_STACK_FIXERS, stack):
        dockerfile = fixer(dockerfile)
    return dockerfile


def collect_stack_issues(dockerfile: str, stack: str | None) -> list[str]:
    issues: list[str] = []
    for validator in _UNIVERSAL_VALIDATORS:
        issues.extend(validator(dockerfile))
    for validator in _resolve_stack_handlers(_STACK_VALIDATORS, stack):
        issues.extend(validator(dockerfile))
    return issues
