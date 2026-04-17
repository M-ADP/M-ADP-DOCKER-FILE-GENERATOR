import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.core.generators import BaseDockerfileGenerator
from src.infra.llm.docker_hub_verifier import DockerHubVerifier
from src.infra.llm.dockerfile_processing import (
    _build_tree_from_store,
    _ensure_from_first,
    _fix_wildcard_copy,
    _merge_env_layers,
    _merge_run_layers,
    _normalize_continuation_lines,
    _normalize_source_path,
    _remove_invalid_lines,
    _remove_missing_optional_copy_sources,
    _resolve_store_path,
    _sanitize_base_images,
    _validate_copy_coverage,
    _validate_dockerfile_against_source,
    _validate_dockerfile_syntax,
    generate_dockerignore,
)
from src.infra.llm.dockerfile_processing.constants import STACK_PATTERNS
from src.infra.llm.dockerfile_processing.prompts import HUMAN_PROMPT, SYSTEM_PROMPT
from src.infra.llm.nova import NovaLLM

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10
MAX_AGENT_RETRIES = 3


def detect_stack(store: dict[str, str]) -> Optional[str]:
    """소스 파일과 package.json 내용을 함께 사용해 스택을 감지합니다."""
    normalized_paths = [p.replace("\\", "/").lstrip("./") for p in store.keys()]
    files = set(Path(p).name for p in normalized_paths)
    package_jsons: list[dict] = []

    for path, content in store.items():
        if Path(path).name != "package.json":
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            package_jsons.append(parsed)

    package_names: set[str] = set()
    package_scripts: set[str] = set()
    for package_json in package_jsons:
        for section_name in ("dependencies", "devDependencies", "peerDependencies"):
            section = package_json.get(section_name, {})
            if isinstance(section, dict):
                package_names.update(section.keys())

        scripts = package_json.get("scripts", {})
        if isinstance(scripts, dict):
            package_scripts.update(scripts.keys())

    scores: dict[str, int] = {}

    def add_score(stack_name: str, score: int) -> None:
        scores[stack_name] = scores.get(stack_name, 0) + score

    def has_package(*names: str) -> bool:
        return any(name in package_names for name in names)

    def has_file(*names: str) -> bool:
        return any(name in files for name in names)

    if has_package("next") or has_file(
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
    ):
        add_score("nextjs", 120)
    if has_package("nuxt") or has_file("nuxt.config.js", "nuxt.config.ts"):
        add_score("nuxt", 120)
    if has_package("astro") or has_file(
        "astro.config.js",
        "astro.config.mjs",
        "astro.config.ts",
    ):
        add_score("astro", 110)
    if has_package("@sveltejs/kit", "svelte") or has_file(
        "svelte.config.js",
        "svelte.config.ts",
    ):
        add_score("svelte", 100)
    if has_package("vue") or has_file("vue.config.js", "vue.config.ts"):
        add_score("vue", 95)
    if has_package("vite") or has_file("vite.config.js", "vite.config.ts"):
        add_score("vite-static", 80)
    if has_package("express", "fastify", "koa", "@nestjs/core", "hapi"):
        add_score("node-server", 85)
    if package_jsons and "build" in package_scripts:
        add_score("node-static", 45)
    elif package_jsons:
        add_score("node-server", 35)

    for stack_name, config in STACK_PATTERNS.items():
        if stack_name in {
            "nextjs",
            "nuxt",
            "vue",
            "svelte",
            "astro",
            "vite-static",
            "node-static",
            "node-server",
        }:
            continue

        for detector in config["detector"]:
            if detector in files:
                add_score(stack_name, config["score"])

        for secondary in config.get("secondary", []):
            if secondary in files and config.get("detector"):
                add_score(stack_name, config["score"] // 4)

    if not scores:
        return None

    return max(scores.items(), key=lambda x: x[1])[0]


def detect_project_root(store: dict[str, str], stack: Optional[str]) -> str:
    """소스 파일 경로에서 프로젝트 루트 디렉토리를 감지합니다.

    Returns:
        project root path with trailing slash (e.g. "pinball/"), or "" if root.
    """
    root_indicators = {
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
    if stack and stack in STACK_PATTERNS:
        config = STACK_PATTERNS[stack]
        for f in config.get("detector", []) + config.get("secondary", []):
            if "/" not in f:
                root_indicators.add(f)

    # dir → depth (number of path segments)
    candidate_dirs: dict[str, int] = {}
    for path in store.keys():
        normalized = _normalize_source_path(path)
        filename = normalized.split("/")[-1] if "/" in normalized else normalized
        if filename not in root_indicators:
            continue
        parent = "/".join(normalized.split("/")[:-1]) if "/" in normalized else ""
        depth = len(parent.split("/")) if parent else 0
        if parent not in candidate_dirs or depth < candidate_dirs[parent]:
            candidate_dirs[parent] = depth

    if not candidate_dirs:
        return ""

    shallowest_dir = min(candidate_dirs.items(), key=lambda x: x[1])[0]
    return (shallowest_dir + "/") if shallowest_dir else ""


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

    def _detect_package_manager(self, store: dict[str, str]) -> str:
        """store의 lockfile/설정 파일을 보고 패키지 매니저를 감지합니다."""
        files = {Path(p).name for p in store.keys()}
        full_paths = set(store.keys())

        has_yarnrc_yml = any(
            Path(p).name in (".yarnrc.yml", ".yarnrc.yaml") for p in full_paths
        )
        if has_yarnrc_yml:
            return "yarn-berry"
        if "yarn.lock" in files:
            return "yarn"
        if "pnpm-lock.yaml" in files:
            return "pnpm"
        if "bun.lockb" in files or "bun.lock" in files:
            return "bun"
        return "npm"

    def _get_pkg_manager_instructions(self, pkg_manager: str) -> str:
        if pkg_manager == "yarn-berry":
            return (
                "**패키지 매니저: Yarn Berry (Yarn 2+) 감지됨**\n"
                "- `RUN corepack enable`을 사용하세요. (npm install -g yarn 금지)\n"
                "- 의존성 설치: `yarn install --immutable` (`--frozen-lockfile` 사용 금지)\n"
                "- 캐시 정리: `yarn cache clean`을 빌드 단계 마지막에 포함\n"
                "- COPY 필수 파일: `package.json`, `yarn.lock`, `.yarnrc.yml`, `.yarn/releases/`"
            )
        elif pkg_manager == "yarn":
            return (
                "**패키지 매니저: Yarn Classic 감지됨**\n"
                "- `RUN npm install -g yarn`으로 설치하세요.\n"
                "- 의존성 설치: `yarn install --frozen-lockfile`\n"
                "- 캐시 정리: `yarn cache clean`을 빌드 단계 마지막에 포함\n"
                "- COPY 필수 파일: `package.json`, `yarn.lock` (pnpm-lock.yaml 등 다른 lockfile 사용 금지)"
            )
        elif pkg_manager == "pnpm":
            return (
                "**패키지 매니저: pnpm 감지됨**\n"
                "- `RUN npm install -g pnpm`으로 설치하세요.\n"
                "- 의존성 설치: `pnpm install --frozen-lockfile`\n"
                "- 캐시 정리: `pnpm store prune`을 빌드 단계 마지막에 포함\n"
                "- COPY 필수 파일: `package.json`, `pnpm-lock.yaml` (yarn.lock 등 다른 lockfile 사용 금지)"
            )
        elif pkg_manager == "bun":
            return (
                "**패키지 매니저: Bun 감지됨**\n"
                "- `RUN npm install -g bun`으로 설치하세요.\n"
                "- 의존성 설치: `bun install --frozen-lockfile`\n"
                "- COPY 필수 파일: `package.json`, `bun.lockb` (또는 bun.lock)"
            )
        else:
            return (
                "**패키지 매니저: npm 감지됨**\n"
                "- 의존성 설치: `npm ci --fetch-retries=5` (package-lock.json이 있을 때만)\n"
                "- 캐시 정리: `npm cache clean --force && rm -rf /root/.npm`을 빌드 단계 마지막에 포함\n"
                "- COPY 필수 파일: `package.json`, `package-lock.json` (있는 경우)"
            )

    def _build_detect_info(
        self, stack: Optional[str], project_root: str = "", store: Optional[dict[str, str]] = None
    ) -> str:
        if stack is None:
            return (
                "감지된 스택 없음. 파일을 직접 분석하여 적절한 Dockerfile을 생성하세요."
            )

        config = self.stack_patterns[stack]
        lines = [f"감지된 스택: {stack}", f"- EXPOSE: {config['expose']}"]

        pkg_manager = self._detect_package_manager(store) if store else "npm"
        lines.append("\n" + self._get_pkg_manager_instructions(pkg_manager))

        if stack == "nextjs":
            lines += [
                f"- CMD: `{pkg_manager} start` (package.json scripts 확인)",
                "- **중요**: Next.js는 standalone 빌드가 필요",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                f"2. **의존성 COPY**: 위 '패키지 매니저' 섹션의 필수 파일을 COPY 하세요.",
                "   - 존재하지 않는 lockfile을 COPY에 포함하지 마세요.",
                "3. **도구 설치 및 의존성 설치**: 위 '패키지 매니저' 지침을 따르세요.",
                "4. **소스 복사 필수**: `COPY . .` 또는 실제 app/pages/src 경로를 build 전에 COPY",
                "5. **레이어 결합 필수**: install/build/cache clean을 한 RUN에 결합",
                "6. **런타임 스테이지**: `FROM node:22-alpine`",
                f"7. **도구 설치 (runner)**: `{pkg_manager}` 실행에 필요한 도구를 설치하세요.",
                "8. **복사**: `COPY --from=builder /app/.next ./.next`",
                "   `COPY --from=builder /app/public ./public`",
                "   `COPY --from=builder /app/node_modules ./node_modules`",
                "   `COPY --from=builder /app/package.json ./package.json`",
                "9. **레이어 결합 필수**: `RUN addgroup -S appgroup && adduser -S appuser -G appgroup && chown -R appuser:appgroup /app`",
                "10. **환경변수 결합**: `ENV NODE_ENV=production`",
                "11. **비루트 사용자**: `USER appuser`",
                f"12. **CMD**: `CMD ['{pkg_manager}', 'start']` (yarn/pnpm의 경우 corepack enable 필요)",
            ]
        elif stack == "nuxt":
            lines += [
                f"- CMD: `{pkg_manager} run start` (또는 적절한 실행 명령어)",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                f"2. **도구 및 의존성 설치**: 위 '{pkg_manager}' 지침을 따르세요.",
                "3. **소스 복사 필수**: build 실행 전에 `COPY . .`",
                "4. **런타임 스테이지**: `FROM node:22-alpine`",
                f"5. **도구 설치 (runner)**: `{pkg_manager}` 실행에 필요한 도구를 설치하세요.",
                "6. **복사**: `COPY --from=builder /app/.output ./.output`",
                "7. **CMD**: `CMD ['node', '.output/server/index.mjs']`",
            ]
        elif stack in ("vue", "svelte", "vite-static"):
            lines += [
                f"- CMD: {config.get('cmd', 'serve -s dist -l 3000')}",
                f"- 빌드: `{pkg_manager} run build` → dist/ 생성",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                f"2. **도구 및 의존성 설치**: 위 '{pkg_manager}' 지침을 따르세요.",
                "3. **소스 복사 필수**: build 실행 전에 `COPY . .`",
                "4. **런타임 스테이지**: `FROM node:22-alpine`",
                "5. **복사**: `COPY --from=builder /app/dist ./dist`",
                "6. **serve 설치 + user**: `RUN npm install -g serve && addgroup -S appgroup && adduser -S appuser -G appgroup`",
                "7. **CMD**: `CMD ['serve', '-s', 'dist', '-l', '3000']`",
            ]
        elif stack == "astro":
            lines += [
                f"- 빌드: `{pkg_manager} run build` → dist/ 생성",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                f"2. **도구 및 의존성 설치**: 위 '{pkg_manager}' 지침을 따르세요.",
                "3. **소스 복사 필수**: build 실행 전에 `COPY . .`",
                "4. **런타임 스테이지**: `FROM node:22-alpine`",
                "5. **복사**: `COPY --from=builder /app/dist ./dist`",
                "6. **serve 설치**: `RUN npm install -g serve`",
                "7. **CMD**: `CMD ['serve', '-s', 'dist', '-l', '3000']`",
            ]
        elif stack == "node-static":
            lines += [
                f"- CMD: {config['cmd']}",
                f"- 빌드: `{pkg_manager} run build` → dist/ 생성",
                "- runner: node:22-alpine + serve",
                f"- runner에서 `{pkg_manager} install --production` 불필요 (정적 파일만 필요)",
                "- nginx 사용 금지 (외부 설정 파일 의존성 위험)",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                f"2. **도구 및 의존성 설치**: 위 '{pkg_manager}' 지침을 따르세요.",
                "3. **소스 복사 필수**: build 실행 전에 `COPY . .`",
                "4. **런타임 스테이지**: `FROM node:22-alpine`",
                "5. **빌드 결과만 복사**: `COPY --from=builder /app/dist ./dist`",
                "6. **레이어 결합**: serve 설치 + user 생성",
                "7. **환경변수 결합**: `ENV NODE_ENV=production`",
                "8. **비루트 사용자**: `USER appuser`",
            ]
        elif stack == "node-server":
            lines += [
                "- CMD: package.json의 main 또는 scripts.start를 read_file로 확인 후 결정",
                f"- runner에서 production 의존성만 포함되도록 설정",
                "",
                "**최적화 가이드 (필수):**",
                "1. **빌드 스테이지**: `FROM node:22-alpine AS builder`",
                f"2. **도구 및 의존성 설치**: 위 '{pkg_manager}' 지침을 따르세요. (`--production` 제외하고 전체 설치)",
                "3. **빌드 또는 소스 준비**: 필요 시 build 수행",
                "4. **런타임 스테이지**: `FROM node:22-alpine`",
                f"5. **production 의존성 설치 (선택)**: builder에서 `{pkg_manager} install --production` 후 node_modules만 복사하거나, runner에서 설치",
                "6. **소스 코드 복사**: `COPY --from=builder /app/src ./src` (또는 필요한 파일만)",
                "7. **레이어 결합**: user 생성 + chown",
                "8. **환경변수 결합**: `ENV NODE_ENV=production`",
                "9. **비루트 사용자**: `USER appuser`",
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

        if project_root:
            lines += [
                "",
                f"⚠️ **프로젝트 루트: `{project_root}`**",
                f"  - 소스코드가 루트가 아닌 `{project_root}` 하위에 있습니다.",
                f"  - 개별 파일 COPY 대신 **`COPY {project_root} .`** 를 사용하여 전체 디렉토리를 복사하세요.",
                f"  - 예: `COPY package.json ./` 이 아닌 `COPY {project_root} .`",
                f"  - 개별 COPY를 사용하면 `{project_root}index.html` 같은 루트 파일이 누락될 수 있습니다.",
            ]

        lines.append("\n위 규칙을 반드시 준수하세요.")
        return "\n".join(lines)

    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        context: str,
    ) -> tuple[str, str, int]:
        stack = detect_stack(store)
        project_root = detect_project_root(store, stack)
        logger.info(
            f"[DockerfileGenerator] detected stack: {stack}, project_root: '{project_root}'"
        )

        verifier = DockerHubVerifier()

        # Pydantic schemas for structured tools
        class ReadFileInput(BaseModel):
            path: str = Field(
                description="소스코드 파일의 경로. 트리에 표시된 경로를 그대로 사용하세요."
            )

        class ListTreeInput(BaseModel):
            path: str = Field(
                default="",
                description="조회할 디렉토리 경로. 루트는 빈 문자열 또는 '.'을 사용하세요.",
            )
            max_depth: int = Field(
                default=3,
                ge=1,
                le=8,
                description="조회할 최대 깊이. 기본값은 3입니다.",
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
            resolved_path = _resolve_store_path(store, path)
            content = store.get(resolved_path) if resolved_path else None
            if content is None:
                return f"[오류] 파일을 찾을 수 없습니다: {path}"
            logger.info(f"[DockerfileGenerator] read_file: {resolved_path}")
            return content

        @tool(args_schema=ListTreeInput)
        def list_tree(path: str = "", max_depth: int = 3) -> str:
            """소스코드 디렉토리 구조를 조회합니다."""
            normalized_path = "" if path in ("", ".") else path
            logger.info(
                "[DockerfileGenerator] list_tree: path=%s, max_depth=%s",
                normalized_path or ".",
                max_depth,
            )
            return _build_tree_from_store(store, normalized_path, max_depth)

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
            [read_file, list_tree, verify_docker_image, search_docker_image]
        )
        messages = [
            SystemMessage(
                content=SYSTEM_PROMPT.format(stack_info=self._build_stack_info())
            ),
            HumanMessage(
                content=HUMAN_PROMPT.format(
                    tree=tree,
                    context=context,
                    detect_info=self._build_detect_info(stack, project_root, store),
                )
            ),
        ]

        dockerfile = await self._run_agent_with_retry(
            llm_with_tools,
            read_file,
            list_tree,
            verify_docker_image,
            search_docker_image,
            messages,
            store,
            stack,
            project_root,
        )
        dockerfile = _remove_missing_optional_copy_sources(dockerfile, store)
        dockerignore = generate_dockerignore(store, stack, dockerfile)
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
        list_tree_tool: Callable,
        verify_image_tool: Callable,
        search_image_tool: Callable,
        messages: list,
        store: dict[str, str],
        stack: Optional[str],
        project_root: str = "",
    ) -> str:
        last_error = None

        for attempt in range(MAX_AGENT_RETRIES):
            try:
                result = await self._run_agent(
                    llm_with_tools,
                    read_file_tool,
                    list_tree_tool,
                    verify_image_tool,
                    search_image_tool,
                    messages,
                )
                source_issues = _validate_dockerfile_against_source(
                    result,
                    store,
                    stack,
                )
                if source_issues:
                    for issue in source_issues:
                        logger.error(f"[Dockerfile] Source issue: {issue}")
                    raise ValueError(
                        f"Dockerfile source validation failed: {'; '.join(source_issues[:3])}"
                    )

                coverage_issues = _validate_copy_coverage(result, store, project_root)
                if coverage_issues:
                    for issue in coverage_issues:
                        logger.error(f"[Dockerfile] Coverage issue: {issue}")
                    raise ValueError(
                        f"Dockerfile COPY coverage incomplete: {'; '.join(coverage_issues[:3])}"
                    )

                return result
            except ValueError as e:
                last_error = e
                logger.warning(
                    f"[DockerfileGenerator] Attempt {attempt + 1}/{MAX_AGENT_RETRIES} failed: {e}"
                )
                if project_root and "coverage" in str(e):
                    retry_msg = (
                        "이전 Dockerfile은 소스 커버리지 검증에 실패했습니다. "
                        f"오류: {e}. "
                        f"프로젝트 루트 '{project_root}'의 모든 파일을 복사하려면 "
                        f"`COPY {project_root} .`을 사용하세요. "
                        "개별 파일 COPY는 index.html 같은 파일을 누락할 수 있습니다. "
                        "list_tree로 실제 파일 목록을 확인하고 Dockerfile만 다시 생성하세요."
                    )
                else:
                    retry_msg = (
                        "이전 Dockerfile은 소스 트리 검증에 실패했습니다. "
                        f"오류: {e}. list_tree/read_file 결과를 다시 반영하여 "
                        "build 전에 필요한 소스 디렉토리가 컨테이너에 복사되도록 "
                        "Dockerfile만 다시 생성하세요."
                    )
                messages.append(HumanMessage(content=retry_msg))
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
        list_tree_tool: Callable,
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
                    elif tool_name == "list_tree":
                        result = list_tree_tool.invoke(tool_args)
                    elif tool_name == "verify_docker_image":
                        result = await verify_image_tool.ainvoke(tool_args)
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
