"""
Deterministic Dockerfile renderer for known stacks.

LLM 대신 검증된 템플릿으로 Dockerfile을 생성한다.
알 수 없는 스택은 None을 반환해 LLM 경로로 폴백한다.
"""
import logging
import re
from pathlib import Path as _Path

from src.core.spec.models import BuildSpec

logger = logging.getLogger(__name__)

_NODE_ALPINE = "node:22-alpine"
_PYTHON_SLIM = "python:3.12-slim"
_GRADLE_IMAGE = "gradle:8-jdk17"
_JRE_IMAGE = "eclipse-temurin:17-jre"
_GO_BUILDER = "golang:1.22-alpine"
_GO_RUNNER = "alpine:3.19"

_ADDUSER_ALPINE = (
    "RUN addgroup -S appgroup && adduser -S -G appgroup -H appuser "
    "&& chown -R appuser:appgroup /app"
)
_ADDUSER_DEBIAN = (
    "RUN groupadd -r appgroup && useradd -r -g appgroup appuser "
    "&& chown -R appuser:appgroup /app"
)

_LOCKFILES: dict[str, str] = {
    "npm": "package-lock.json",
    "pnpm": "pnpm-lock.yaml",
    "yarn": "yarn.lock",
    "yarn-berry": "yarn.lock",
    "bun": "bun.lockb",
}

_PKG_SETUP: dict[str, str] = {
    "pnpm": "corepack enable",
    "yarn-berry": "corepack enable",
    "yarn": "npm install -g yarn",
    "bun": "npm install -g bun",
}

_STATIC_SERVE_STACKS = frozenset({"node-static", "vite-static", "astro", "vue", "svelte"})


class TemplateRenderer:
    """알려진 스택에 대해 결정론적 Dockerfile을 생성한다."""

    SUPPORTED_STACKS = frozenset({
        "nextjs", "nuxt",
        "node-static", "vite-static", "astro", "vue", "svelte",
        "python-fastapi", "python-flask",
        "java-gradle", "java-maven",
        "go",
    })

    def can_render(self, stack: str | None) -> bool:
        return bool(stack) and stack in self.SUPPORTED_STACKS

    def render(self, spec: BuildSpec, store: dict[str, str]) -> str:
        stack = spec.detected_stack
        logger.info("[TemplateRenderer] stack=%s pkg=%s", stack, spec.pkg_manager)

        if stack == "nextjs":
            return self._nextjs(spec, store)
        if stack == "nuxt":
            return self._nuxt(spec, store)
        if stack in _STATIC_SERVE_STACKS:
            return self._node_static(spec, store)
        if stack in ("python-fastapi", "python-flask"):
            return self._python(spec, store)
        if stack == "java-gradle":
            return self._java_gradle(spec, store)
        if stack == "java-maven":
            return self._java_maven(spec, store)
        if stack == "go":
            return self._go(spec, store)
        raise ValueError(f"[TemplateRenderer] 지원하지 않는 스택: {stack}")

    # ── 공통 헬퍼 ─────────────────────────────────────────────────────────────

    def _node_run(self, spec: BuildSpec) -> str:
        """setup && install && build 를 하나의 RUN 명령으로 합친다."""
        pkg = spec.pkg_manager
        parts: list[str] = []
        if setup := _PKG_SETUP.get(pkg, ""):
            parts.append(setup)
        parts.append(self._clean_install(spec))
        if build := (spec.pkg_manager_build_cmd or ""):
            parts.append(build)
        return " && ".join(parts)

    def _clean_install(self, spec: BuildSpec) -> str:
        """install_cmd에서 setup 접두사를 제거한다 (템플릿이 직접 추가)."""
        cmd = spec.pkg_manager_install_cmd
        cmd = re.sub(r"^corepack\s+enable\s*&&\s*", "", cmd).strip()
        cmd = re.sub(r"^npm\s+(?:install|i)\s+-g\s+\S+\s*&&\s*", "", cmd).strip()
        return cmd

    def _copy_src(self, root: str, pkg: str, store: dict) -> list[str]:
        """builder 스테이지용 COPY 라인 목록 (의존성 → 소스 두 단계)."""
        lf = _LOCKFILES.get(pkg, "")
        lines: list[str] = []

        # 1단계: 매니페스트 파일 (캐시 레이어)
        lf_path = f"{root}{lf}" if lf else ""
        if lf_path and lf_path in store:
            lines.append(f"COPY {root}package.json {lf_path} ./")
        else:
            lines.append(f"COPY {root}package.json ./")

        if pkg == "yarn-berry":
            if f"{root}.yarnrc.yml" in store:
                lines.append(f"COPY {root}.yarnrc.yml ./")
            if any(k.startswith(f"{root}.yarn/releases/") for k in store):
                lines.append(f"COPY {root}.yarn/releases/ .yarn/releases/")

        # 2단계: 전체 소스
        src = f"{root}." if root else "."
        lines.append(f"COPY {src} .")
        return lines

    def _env_line(self, env: dict[str, str], base: dict[str, str] | None = None) -> str | None:
        merged = {**(base or {}), **env}
        if not merged:
            return None
        return "ENV " + " ".join(f"{k}={v}" for k, v in merged.items())

    def _runner_env(self, spec: BuildSpec) -> dict[str, str]:
        for s in spec.stages:
            if s.name == "runner":
                return s.env_vars
        return {}

    def _port(self, spec: BuildSpec, default: int) -> int:
        for s in spec.stages:
            if s.expose_port:
                return s.expose_port
        return default

    def _is_standalone(self, store: dict) -> bool:
        for name, content in store.items():
            if _Path(name).name in (
                "next.config.ts", "next.config.js",
                "next.config.mjs", "next.config.cjs",
            ):
                if re.search(r"""output\s*:\s*['"]standalone['"]""", content):
                    return True
        return False

    def _has_dir(self, store: dict, root: str, dir_name: str) -> bool:
        prefix = f"{root}{dir_name}/"
        return any(k.startswith(prefix) for k in store)

    def _lines_to_str(self, lines: list[str | None]) -> str:
        return "\n".join(ln for ln in lines if ln is not None)

    # ── 스택별 렌더러 ────────────────────────────────────────────────────────

    def _nextjs(self, spec: BuildSpec, store: dict) -> str:
        root = spec.project_root
        standalone = self._is_standalone(store)
        port = self._port(spec, 3000)
        env = self._env_line(self._runner_env(spec), {"NODE_ENV": "production"})
        has_public = self._has_dir(store, root, "public")
        public_copy = "COPY --from=builder /app/public ./public" if has_public else None

        builder = [
            f"FROM {_NODE_ALPINE} AS builder",
            "WORKDIR /app",
            *self._copy_src(root, spec.pkg_manager, store),
            f"RUN {self._node_run(spec)}",
        ]

        if standalone:
            runner_copies = [
                "COPY --from=builder /app/.next/standalone ./",
                "COPY --from=builder /app/.next/static ./.next/static",
                public_copy,
            ]
            cmd = '["node", "server.js"]'
        else:
            runner_copies = [
                "COPY --from=builder /app/.next ./.next",
                "COPY --from=builder /app/node_modules ./node_modules",
                "COPY --from=builder /app/package.json ./package.json",
                public_copy,
            ]
            cmd = '["node_modules/.bin/next", "start"]'

        runner = [
            f"FROM {_NODE_ALPINE} AS runner",
            "WORKDIR /app",
            *[ln for ln in runner_copies if ln],
            _ADDUSER_ALPINE,
            env,
            "USER appuser",
            f"EXPOSE {port}",
            f"CMD {cmd}",
        ]

        return self._lines_to_str(builder) + "\n\n" + self._lines_to_str(runner)

    def _nuxt(self, spec: BuildSpec, store: dict) -> str:
        root = spec.project_root
        port = self._port(spec, 3000)
        env = self._env_line(self._runner_env(spec), {"NODE_ENV": "production", "HOST": "0.0.0.0", "PORT": str(port)})

        builder = [
            f"FROM {_NODE_ALPINE} AS builder",
            "WORKDIR /app",
            *self._copy_src(root, spec.pkg_manager, store),
            f"RUN {self._node_run(spec)}",
        ]
        runner = [
            f"FROM {_NODE_ALPINE} AS runner",
            "WORKDIR /app",
            "COPY --from=builder /app/.output ./.output",
            _ADDUSER_ALPINE,
            env,
            "USER appuser",
            f"EXPOSE {port}",
            '["node", ".output/server/index.mjs"]',
        ]
        # Nuxt CMD needs CMD prefix
        runner[-1] = f"CMD {runner[-1]}"

        return self._lines_to_str(builder) + "\n\n" + self._lines_to_str(runner)

    def _node_static(self, spec: BuildSpec, store: dict) -> str:
        root = spec.project_root
        port = self._port(spec, 3000)

        # vite/astro → dist, next static export → out
        dist_dir = "out" if spec.detected_stack == "astro" else "dist"

        builder = [
            f"FROM {_NODE_ALPINE} AS builder",
            "WORKDIR /app",
            *self._copy_src(root, spec.pkg_manager, store),
            f"RUN {self._node_run(spec)}",
        ]
        runner = [
            f"FROM {_NODE_ALPINE} AS runner",
            "WORKDIR /app",
            f"COPY --from=builder /app/{dist_dir} ./{dist_dir}",
            f"RUN npm install -g serve && {_ADDUSER_ALPINE[4:]}",
            "ENV NODE_ENV=production",
            "USER appuser",
            f"EXPOSE {port}",
            f'CMD ["serve", "-s", "{dist_dir}", "-l", "{port}"]',
        ]

        return self._lines_to_str(builder) + "\n\n" + self._lines_to_str(runner)

    def _python(self, spec: BuildSpec, store: dict) -> str:
        root = spec.project_root
        port = self._port(spec, 8000)
        stack = spec.detected_stack
        is_fastapi = (stack == "python-fastapi")

        # requirements 파일 감지
        req_file = "requirements.txt"
        if f"{root}pyproject.toml" in store and f"{root}requirements.txt" not in store:
            req_file = "pyproject.toml"

        install_cmd = spec.pkg_manager_install_cmd or f"pip install --no-cache-dir -r {req_file}"

        # CMD 결정: spec runner stage → STACK_PATTERNS 기본값
        cmd_list = self._runner_cmd(spec)
        if not cmd_list:
            if is_fastapi:
                cmd_list = ["uvicorn", "main:app", "--host", "0.0.0.0", f"--port", str(port)]
            else:
                cmd_list = ["python", "-m", "flask", "run", "--host", "0.0.0.0"]
        cmd = "[" + ", ".join(f'"{c}"' for c in cmd_list) + "]"

        uvicorn_copy = (
            "COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn"
            if is_fastapi else None
        )

        env = self._env_line(
            self._runner_env(spec),
            {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
        )

        builder = [
            f"FROM {_PYTHON_SLIM} AS builder",
            "WORKDIR /app",
            f"COPY {root}{req_file} ./",
            f"RUN {install_cmd}",
            f"COPY {root}. ." if root else "COPY . .",
        ]
        runner = [
            f"FROM {_PYTHON_SLIM} AS runner",
            "WORKDIR /app",
            env,
            "COPY --from=builder /usr/local/lib/python3.12/site-packages "
            "/usr/local/lib/python3.12/site-packages",
            uvicorn_copy,
            "COPY --from=builder /app ./",
            _ADDUSER_DEBIAN,
            "USER appuser",
            f"EXPOSE {port}",
            f"CMD {cmd}",
        ]

        return self._lines_to_str(builder) + "\n\n" + self._lines_to_str(runner)

    def _java_gradle(self, spec: BuildSpec, store: dict) -> str:
        port = self._port(spec, 8080)
        build_cmd = spec.pkg_manager_build_cmd or "gradle clean bootJar --no-daemon -x test"
        # 와일드카드 없이 JAR 복사
        jar_copy = (
            f"RUN {build_cmd} && "
            r"find build/libs -name '*.jar' ! -name '*-plain.jar' -exec cp {} /app.jar \;"
        )

        builder = [
            f"FROM {_GRADLE_IMAGE} AS builder",
            "WORKDIR /app",
            "COPY . .",
            jar_copy,
        ]
        runner = [
            f"FROM {_JRE_IMAGE} AS runner",
            "WORKDIR /app",
            "COPY --from=builder /app.jar /app.jar",
            _ADDUSER_DEBIAN,
            "USER appuser",
            f"EXPOSE {port}",
            '["java", "-jar", "/app.jar"]',
        ]
        runner[-1] = f"CMD {runner[-1]}"

        return self._lines_to_str(builder) + "\n\n" + self._lines_to_str(runner)

    def _java_maven(self, spec: BuildSpec, store: dict) -> str:
        port = self._port(spec, 8080)
        build_cmd = spec.pkg_manager_build_cmd or "mvn clean package -DskipTests --no-transfer-progress"

        builder = [
            "FROM eclipse-temurin:17-jdk AS builder",
            "WORKDIR /app",
            "COPY pom.xml ./",
            "RUN mvn dependency:go-offline -q || true",
            "COPY src/ src/",
            f"RUN {build_cmd} && "
            r"find target -maxdepth 1 -name '*.jar' ! -name '*-sources.jar' -exec cp {} /app.jar \;",
        ]
        runner = [
            f"FROM {_JRE_IMAGE} AS runner",
            "WORKDIR /app",
            "COPY --from=builder /app.jar /app.jar",
            _ADDUSER_DEBIAN,
            "USER appuser",
            f"EXPOSE {port}",
            'CMD ["java", "-jar", "/app.jar"]',
        ]

        return self._lines_to_str(builder) + "\n\n" + self._lines_to_str(runner)

    def _go(self, spec: BuildSpec, store: dict) -> str:
        port = self._port(spec, 8080)

        # go.sum이 있으면 COPY
        has_sum = "go.sum" in store
        mod_copy = "COPY go.mod go.sum ./" if has_sum else "COPY go.mod ./"

        builder = [
            f"FROM {_GO_BUILDER} AS builder",
            "WORKDIR /app",
            mod_copy,
            "RUN go mod download",
            "COPY . .",
            "RUN CGO_ENABLED=0 GOOS=linux go build -o /app/server .",
        ]
        runner = [
            f"FROM {_GO_RUNNER} AS runner",
            "RUN apk add --no-cache ca-certificates",
            "WORKDIR /app",
            "COPY --from=builder /app/server /app/server",
            "RUN addgroup -S appgroup && adduser -S -G appgroup -H appuser "
            "&& chown -R appuser:appgroup /app",
            "USER appuser",
            f"EXPOSE {port}",
            'CMD ["/app/server"]',
        ]

        return self._lines_to_str(builder) + "\n\n" + self._lines_to_str(runner)

    def _runner_cmd(self, spec: BuildSpec) -> list[str]:
        """runner 스테이지의 cmd 필드를 반환한다."""
        for s in spec.stages:
            if s.name == "runner" and s.cmd:
                return s.cmd
        return []
