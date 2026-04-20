"""
결정론적 Dockerfile 템플릿 — BuildParams를 받아 구조적으로 올바른 Dockerfile 생성.
LLM 없음, fixer 없음.
"""
from src.core.params.models import BuildParams

_ADDUSER_ALPINE = (
    "RUN addgroup -S appgroup && adduser -S -G appgroup -H appuser "
    "&& chown -R appuser:appgroup /app"
)
_ADDUSER_DEBIAN = (
    "RUN groupadd -r appgroup && useradd -r -g appgroup appuser "
    "&& chown -R appuser:appgroup /app"
)


def _lines(*parts: str | None) -> str:
    return "\n".join(p for p in parts if p is not None)


def _cmd(cmd_list: list[str]) -> str:
    return "CMD [" + ", ".join(f'"{c}"' for c in cmd_list) + "]"


def _env(base: dict[str, str], extra: dict[str, str] | None = None) -> str | None:
    merged = {**base, **(extra or {})}
    if not merged:
        return None
    return "ENV " + " ".join(f"{k}={v}" for k, v in merged.items())


def _run(*cmds: str | None) -> str:
    parts = [c for c in cmds if c]
    return "RUN " + " && \\\n    ".join(parts)


# ── Node 공통 헬퍼 ────────────────────────────────────────────────────────────

def _node_copy_src(params: BuildParams) -> list[str]:
    """builder 스테이지용 COPY 라인 목록 (캐시 레이어 → 소스 두 단계)."""
    d    = params.detected
    root = d.project_root
    lf   = d.lockfile
    lines: list[str] = []

    # 1단계: 매니페스트 (캐시 레이어)
    if lf:
        lines.append(f"COPY {root}package.json {root}{lf} ./")
    else:
        lines.append(f"COPY {root}package.json ./")

    # yarn-berry 추가 파일
    if d.package_manager == "yarn-berry":
        if d.has_yarnrc:
            lines.append(f"COPY {root}.yarnrc.yml ./")
        if d.has_yarn_releases:
            lines.append(f"COPY {root}.yarn/releases/ .yarn/releases/")

    # 2단계: 전체 소스
    lines.append(f"COPY {root}. ." if root else "COPY . .")
    return lines


def _node_build_run(params: BuildParams) -> str:
    """install + build 를 하나의 RUN으로."""
    cmds = [params.install_cmd.value]
    if params.build_cmd.value:
        cmds.append(params.build_cmd.value)
    return "RUN " + " && \\\n    ".join(cmds)


# ── Python 템플릿 ─────────────────────────────────────────────────────────────

def render_python(params: BuildParams) -> str:
    d        = params.detected
    root     = d.project_root
    req      = d.req_file or "requirements.txt"
    port     = params.port.value
    is_fastapi = (d.framework == "python-fastapi")
    base     = params.base_image.value
    runner   = params.runner_image.value

    builder = _lines(
        f"FROM {base} AS builder",
        "WORKDIR /app",
        f"COPY {root}{req} ./",
        f"RUN {params.install_cmd.value}",
        f"COPY {root}. ." if root else "COPY . .",
    )
    runner_lines = [
        f"FROM {runner} AS runner",
        "WORKDIR /app",
        _env({}, {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}),
        "COPY --from=builder /usr/local/lib/python3.12/site-packages "
        "/usr/local/lib/python3.12/site-packages",
        "COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn" if is_fastapi else None,
        "COPY --from=builder /app ./",
        _ADDUSER_DEBIAN,
        f"EXPOSE {port}",
        "USER appuser",
        _cmd(params.start_cmd.value),
    ]
    return builder + "\n\n" + _lines(*runner_lines)


# ── Next.js 템플릿 (non-standalone) ──────────────────────────────────────────

def render_nextjs(params: BuildParams) -> str:
    d      = params.detected
    root   = d.project_root
    port   = params.port.value
    base   = params.base_image.value
    runner = params.runner_image.value

    public_copy = "COPY --from=builder /app/public ./public" if d.has_public_dir else None

    builder = _lines(
        f"FROM {base} AS builder",
        "WORKDIR /app",
        *_node_copy_src(params),
        _node_build_run(params),
    )
    runner_lines = [
        f"FROM {runner} AS runner",
        "WORKDIR /app",
        "COPY --from=builder /app/.next ./.next",
        "COPY --from=builder /app/node_modules ./node_modules",
        "COPY --from=builder /app/package.json ./package.json",
        public_copy,
        _ADDUSER_ALPINE,
        _env({}, {"NODE_ENV": "production"}),
        f"EXPOSE {port}",
        "USER appuser",
        _cmd(params.start_cmd.value),
    ]
    return builder + "\n\n" + _lines(*runner_lines)


# ── Next.js Standalone 템플릿 ─────────────────────────────────────────────────

def render_nextjs_standalone(params: BuildParams) -> str:
    d      = params.detected
    root   = d.project_root
    port   = params.port.value
    base   = params.base_image.value
    runner = params.runner_image.value

    public_copy = "COPY --from=builder /app/public ./public" if d.has_public_dir else None

    builder = _lines(
        f"FROM {base} AS builder",
        "WORKDIR /app",
        *_node_copy_src(params),
        _node_build_run(params),
    )
    runner_lines = [
        f"FROM {runner} AS runner",
        "WORKDIR /app",
        "COPY --from=builder /app/.next/standalone ./",
        "COPY --from=builder /app/.next/static ./.next/static",
        public_copy,
        _ADDUSER_ALPINE,
        _env({}, {"NODE_ENV": "production"}),
        f"EXPOSE {port}",
        "USER appuser",
        _cmd(params.start_cmd.value),
    ]
    return builder + "\n\n" + _lines(*runner_lines)


# ── Nuxt 템플릿 ────────────────────────────────────────────────────────────────

def render_nuxt(params: BuildParams) -> str:
    d      = params.detected
    port   = params.port.value
    base   = params.base_image.value
    runner = params.runner_image.value

    builder = _lines(
        f"FROM {base} AS builder",
        "WORKDIR /app",
        *_node_copy_src(params),
        _node_build_run(params),
    )
    runner_lines = [
        f"FROM {runner} AS runner",
        "WORKDIR /app",
        "COPY --from=builder /app/.output ./.output",
        _ADDUSER_ALPINE,
        _env({}, {"NODE_ENV": "production", "HOST": "0.0.0.0", "PORT": str(port)}),
        f"EXPOSE {port}",
        "USER appuser",
        _cmd(params.start_cmd.value),
    ]
    return builder + "\n\n" + _lines(*runner_lines)


# ── Node server 템플릿 ────────────────────────────────────────────────────────

def render_node_server(params: BuildParams) -> str:
    d      = params.detected
    port   = params.port.value
    base   = params.base_image.value
    runner = params.runner_image.value

    builder = _lines(
        f"FROM {base} AS builder",
        "WORKDIR /app",
        *_node_copy_src(params),
        _node_build_run(params),
    )
    runner_lines = [
        f"FROM {runner} AS runner",
        "WORKDIR /app",
        "COPY --from=builder /app/node_modules ./node_modules",
        "COPY --from=builder /app/package.json ./package.json",
        "COPY --from=builder /app ./",
        _ADDUSER_ALPINE,
        _env({}, {"NODE_ENV": "production"}),
        f"EXPOSE {port}",
        "USER appuser",
        _cmd(params.start_cmd.value),
    ]
    return builder + "\n\n" + _lines(*runner_lines)


# ── Static 템플릿 (Vite / Vue / Svelte / Astro / node-static) ─────────────────

def render_static(params: BuildParams) -> str:
    d           = params.detected
    port        = params.port.value
    base        = params.base_image.value
    runner      = params.runner_image.value
    build_out   = params.build_output.value or "dist"

    builder = _lines(
        f"FROM {base} AS builder",
        "WORKDIR /app",
        *_node_copy_src(params),
        _node_build_run(params),
    )
    runner_lines = [
        f"FROM {runner} AS runner",
        "WORKDIR /app",
        f"COPY --from=builder /app/{build_out} ./{build_out}",
        f"RUN npm install -g serve && {_ADDUSER_ALPINE[4:]}",  # 'RUN ' 제거 후 이어붙임
        _env({}, {"NODE_ENV": "production"}),
        f"EXPOSE {port}",
        "USER appuser",
        f'CMD ["serve", "-s", "{build_out}", "-l", "{port}"]',
    ]
    return builder + "\n\n" + _lines(*runner_lines)


# ── Go 템플릿 ─────────────────────────────────────────────────────────────────

def render_go(params: BuildParams) -> str:
    d      = params.detected
    port   = params.port.value
    base   = params.base_image.value
    runner = params.runner_image.value

    mod_copy = "COPY go.mod go.sum ./" if d.has_go_sum else "COPY go.mod ./"

    builder = _lines(
        f"FROM {base} AS builder",
        "WORKDIR /app",
        mod_copy,
        "RUN go mod download",
        "COPY . .",
        f"RUN {params.build_cmd.value}",
    )
    runner_lines = [
        f"FROM {runner} AS runner",
        "RUN apk add --no-cache ca-certificates",
        "WORKDIR /app",
        "COPY --from=builder /app/server /app/server",
        "RUN addgroup -S appgroup && adduser -S -G appgroup -H appuser "
        "&& chown -R appuser:appgroup /app",
        f"EXPOSE {port}",
        "USER appuser",
        _cmd(params.start_cmd.value),
    ]
    return builder + "\n\n" + _lines(*runner_lines)


# ── Java 템플릿 (Gradle / Maven 공통) ────────────────────────────────────────

def render_java(params: BuildParams) -> str:
    d      = params.detected
    port   = params.port.value
    base   = params.base_image.value
    runner = params.runner_image.value

    builder = _lines(
        f"FROM {base} AS builder",
        "WORKDIR /app",
        "COPY . .",
        f"RUN {params.install_cmd.value}",
    )
    runner_lines = [
        f"FROM {runner} AS runner",
        "WORKDIR /app",
        "COPY --from=builder /app.jar /app.jar",
        _ADDUSER_DEBIAN,
        f"EXPOSE {port}",
        "USER appuser",
        _cmd(params.start_cmd.value),
    ]
    return builder + "\n\n" + _lines(*runner_lines)
