import json
import logging
import re
from pathlib import Path

from src.core.params.models import BuildParams, DetectedParams, RuleResult

logger = logging.getLogger(__name__)

# ── 이미지 테이블 ─────────────────────────────────────────────────────────────

_BASE_IMAGES: dict[str, str] = {
    "nextjs":        "node:22-alpine",
    "nuxt":          "node:22-alpine",
    "node-server":   "node:22-alpine",
    "node-static":   "node:22-alpine",
    "vite-static":   "node:22-alpine",
    "astro":         "node:22-alpine",
    "vue":           "node:22-alpine",
    "svelte":        "node:22-alpine",
    "python-fastapi":"python:3.12-slim",
    "python-flask":  "python:3.12-slim",
    "python":        "python:3.12-slim",
    "java-gradle":   "gradle:8-jdk17",
    "java-maven":    "maven:3.9-eclipse-temurin-17",
    "go":            "golang:1.22-alpine",
    "rust":          "rust:1.75-slim",
    "ruby":          "ruby:3.2-slim",
}

_RUNNER_IMAGES: dict[str, str] = {
    "nextjs":        "node:22-alpine",
    "nuxt":          "node:22-alpine",
    "node-server":   "node:22-alpine",
    "node-static":   "node:22-alpine",
    "vite-static":   "node:22-alpine",
    "astro":         "node:22-alpine",
    "vue":           "node:22-alpine",
    "svelte":        "node:22-alpine",
    "python-fastapi":"python:3.12-slim",
    "python-flask":  "python:3.12-slim",
    "python":        "python:3.12-slim",
    "java-gradle":   "eclipse-temurin:17-jre",
    "java-maven":    "eclipse-temurin:17-jre",
    "go":            "alpine:3.19",
    "rust":          "debian:bookworm-slim",
    "ruby":          "ruby:3.2-slim",
}

_DEFAULT_PORTS: dict[str, int] = {
    "nextjs":        3000,
    "nuxt":          3000,
    "node-server":   3000,
    "node-static":   3000,
    "vite-static":   3000,
    "astro":         3000,
    "vue":           3000,
    "svelte":        3000,
    "python-fastapi":8000,
    "python-flask":  5000,
    "python":        8000,
    "java-gradle":   8080,
    "java-maven":    8080,
    "go":            8080,
    "rust":          8080,
    "ruby":          3000,
}

_BUILD_OUTPUTS: dict[str, str] = {
    "nextjs":       ".next",
    "nuxt":         ".output",
    "astro":        "dist",
    "vue":          "dist",
    "svelte":       "dist",
    "vite-static":  "dist",
    "node-static":  "dist",
}


class RuleEngine:
    def resolve(self, detected: DetectedParams, store: dict[str, str]) -> BuildParams:
        scripts = self._get_scripts(store, detected.project_root)

        params = BuildParams(
            detected     = detected,
            base_image   = self._base_image(detected),
            runner_image = self._runner_image(detected),
            install_cmd  = self._install_cmd(detected),
            build_cmd    = self._build_cmd(detected, scripts),
            start_cmd    = self._start_cmd(detected, scripts),
            port         = self._port(detected),
            build_output = self._build_output(detected),
        )

        logger.info(
            "[RuleEngine] install=%s(%s) build=%s(%s) start=%s(%s) port=%s(%s)",
            params.install_cmd.value,  params.install_cmd.confidence,
            params.build_cmd.value,    params.build_cmd.confidence,
            params.start_cmd.value,    params.start_cmd.confidence,
            params.port.value,         params.port.confidence,
        )
        return params

    # ── 각 필드 규칙 ──────────────────────────────────────────────────────────

    @staticmethod
    def _base_image(d: DetectedParams) -> RuleResult[str]:
        if d.framework and d.framework in _BASE_IMAGES:
            return RuleResult(_BASE_IMAGES[d.framework], "certain", f"framework:{d.framework}")
        return RuleResult("ubuntu:22.04", "inferred", "default")

    @staticmethod
    def _runner_image(d: DetectedParams) -> RuleResult[str]:
        if d.framework and d.framework in _RUNNER_IMAGES:
            return RuleResult(_RUNNER_IMAGES[d.framework], "certain", f"framework:{d.framework}")
        return RuleResult("ubuntu:22.04", "inferred", "default")

    @staticmethod
    def _install_cmd(d: DetectedParams) -> RuleResult[str]:
        pkg  = d.package_manager
        lock = d.lockfile

        if pkg == "pnpm":
            inner = "pnpm install --frozen-lockfile" if lock else "pnpm install"
            return RuleResult(f"corepack enable && {inner}", "certain", f"lockfile:{lock}")
        if pkg == "npm":
            cmd = "npm ci" if lock else "npm install"
            return RuleResult(cmd, "certain", f"lockfile:{lock}" if lock else "rule:npm")
        if pkg == "yarn-berry":
            return RuleResult("corepack enable && yarn install --immutable", "certain", "lockfile:yarn.lock+.yarnrc.yml")
        if pkg == "yarn":
            cmd = "yarn install --frozen-lockfile" if lock else "yarn install"
            return RuleResult(cmd, "certain", "lockfile:yarn.lock")
        if pkg == "bun":
            return RuleResult("npm install -g bun && bun install", "certain", "lockfile:bun.lockb")
        if pkg == "pip":
            req = d.req_file or "requirements.txt"
            return RuleResult(f"pip install --no-cache-dir -r {req}", "certain", f"file:{req}")
        if pkg == "gradle":
            return RuleResult(
                r"gradle clean bootJar --no-daemon -x test && find build/libs -name '*.jar' ! -name '*-plain.jar' -exec cp {} /app.jar \;",
                "certain", "framework:java-gradle",
            )
        if pkg == "maven":
            return RuleResult(
                r"mvn clean package -DskipTests --no-transfer-progress && find target -maxdepth 1 -name '*.jar' ! -name '*-sources.jar' -exec cp {} /app.jar \;",
                "certain", "framework:java-maven",
            )
        if pkg == "go":
            return RuleResult("go mod download", "certain", "file:go.mod")
        if pkg == "cargo":
            return RuleResult(
                "cargo build --release && find target/release -maxdepth 1 -type f -executable -exec cp {} /app/server \\;",
                "certain", "file:Cargo.toml",
            )
        if pkg == "bundler":
            return RuleResult(
                "bundle config set --local without 'development test' && bundle install --jobs 4 --retry 3",
                "certain", "file:Gemfile",
            )
        if pkg == "composer":
            return RuleResult("composer install --no-dev", "certain", "file:composer.json")

        return RuleResult("npm install", "inferred", "rule:default")

    @staticmethod
    def _build_cmd(d: DetectedParams, scripts: dict[str, str]) -> RuleResult[str | None]:
        fw = d.framework

        # Go/Java: 빌드가 install_cmd에 포함
        if fw in ("go", "java-gradle", "java-maven", "rust"):
            if fw == "go":
                return RuleResult(
                    "CGO_ENABLED=0 GOOS=linux go build -o /app/server .",
                    "certain", "rule:go",
                )
            return RuleResult(None, "certain", "rule:build-in-install")

        # Node 계열: package.json scripts.build
        if d.package_manager in ("npm", "pnpm", "yarn", "yarn-berry", "bun"):
            prefix = d.package_manager.split("-")[0]  # yarn-berry → yarn
            if "build" in scripts:
                return RuleResult(f"{prefix} run build", "certain", "package.json:scripts.build")
            return RuleResult(None, "inferred", "rule:no-build-script")

        return RuleResult(None, "inferred", "rule:no-build")

    @staticmethod
    def _start_cmd(d: DetectedParams, scripts: dict[str, str]) -> RuleResult[list[str]]:
        fw = d.framework

        if fw == "nextjs":
            if d.standalone:
                return RuleResult(["node", "server.js"], "certain", "rule:nextjs-standalone")
            return RuleResult(["node_modules/.bin/next", "start"], "certain", "rule:nextjs")

        if fw == "nuxt":
            return RuleResult(["node", ".output/server/index.mjs"], "certain", "rule:nuxt")

        if fw == "python-fastapi":
            if d.entry_point:
                module = str(Path(d.entry_point).with_suffix("")).replace("/", ".")
            else:
                module = "main"
            port   = _DEFAULT_PORTS.get(fw, 8000)
            return RuleResult(
                ["uvicorn", f"{module}:app", "--host", "0.0.0.0", "--port", str(port)],
                "inferred", f"rule:fastapi+entry:{d.entry_point}",
            )

        if fw == "python-flask":
            return RuleResult(
                ["python", "-m", "flask", "run", "--host", "0.0.0.0"],
                "inferred", "rule:flask",
            )

        if fw == "python":
            ep = d.entry_point or "main.py"
            return RuleResult(["python", ep], "inferred", f"rule:python+entry:{ep}")

        if fw == "go":
            return RuleResult(["/app/server"], "certain", "rule:go")

        if fw == "rust":
            return RuleResult(["/app/server"], "certain", "rule:rust")

        if fw == "ruby":
            ep = d.entry_point or "app.rb"
            return RuleResult(["bundle", "exec", "ruby", ep], "inferred", f"rule:ruby+entry:{ep}")

        if fw in ("java-gradle", "java-maven"):
            return RuleResult(["java", "-jar", "/app.jar"], "certain", "rule:java")

        if d.runtime == "static":
            return RuleResult([], "certain", "rule:static")  # 템플릿이 serve 명령 고정

        # Node server/static: package.json scripts 확인
        if d.package_manager in ("npm", "pnpm", "yarn", "yarn-berry", "bun"):
            prefix = d.package_manager.split("-")[0]
            if "start" in scripts:
                return RuleResult([prefix, "run", "start"], "inferred", "package.json:scripts.start")
            ep = d.entry_point or "index.js"
            return RuleResult(["node", ep], "inferred", f"rule:node+entry:{ep}")

        return RuleResult([], "unknown", "rule:unknown")

    @staticmethod
    def _port(d: DetectedParams) -> RuleResult[int]:
        if d.framework and d.framework in _DEFAULT_PORTS:
            return RuleResult(_DEFAULT_PORTS[d.framework], "inferred", f"rule:{d.framework}")
        return RuleResult(8080, "inferred", "rule:default")

    @staticmethod
    def _build_output(d: DetectedParams) -> RuleResult[str | None]:
        fw = d.framework
        if fw in _BUILD_OUTPUTS:
            return RuleResult(_BUILD_OUTPUTS[fw], "certain", f"rule:{fw}")
        return RuleResult(None, "inferred", "rule:no-output")

    # ── 유틸 ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_scripts(store: dict[str, str], root: str) -> dict[str, str]:
        for path, content in store.items():
            norm = path.replace("\\", "/").lstrip("./")
            if norm == f"{root}package.json" or Path(norm).name == "package.json":
                try:
                    data = json.loads(content)
                    return data.get("scripts") or {}
                except (json.JSONDecodeError, AttributeError):
                    pass
        return {}
