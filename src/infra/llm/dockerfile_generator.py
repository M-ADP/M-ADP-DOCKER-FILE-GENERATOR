import logging
import re
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from src.core.generators import BaseDockerfileGenerator
from src.infra.llm.nova import NovaLLM

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10

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
    "yarn.lock",
    "package-lock.json",
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
    else:
        if "node_modules" in dirs or "package.json" in files:
            lines.extend(DOCKERIGNORE_NODE)
        if (
            "__pycache__" in dirs
            or "requirements.txt" in files
            or "pyproject.toml" in files
        ):
            lines.extend(DOCKERIGNORE_PYTHON)

    if ".dockerignore" in files:
        lines.insert(0, "# 기존 .dockerignore 참고하여 생성됨")

    return "\n".join(lines)


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
            pending_commands.append(cmd)
        else:
            flush_runs()
            merged_lines.append(line)

    flush_runs()
    return "\n".join(merged_lines)


STACK_PATTERNS = {
    # vite.config.* 또는 next.config.* 가 있으면 정적 빌드 프론트엔드로 확정
    "node-static": {
        "detector": [
            "vite.config.js",
            "vite.config.ts",
            "next.config.js",
            "next.config.ts",
            "svelte.config.js",
            "astro.config.js",
            "astro.config.ts",
        ],
        "expose": "3000",
        "cmd": "serve -s dist -l 3000",
    },
    # server.js / app.js / index.js 가 루트에 있으면 Node 서버로 판단
    "node-server": {
        "detector": ["server.js", "app.js", "index.js"],
        "expose": "3000",
        "cmd": None,  # 엔트리포인트는 LLM이 package.json main/scripts.start에서 확인
    },
    "python-fastapi": {
        "detector": ["main.py"],
        "expose": "8000",
        "cmd": "uvicorn main:app --host 0.0.0.0 --port 8000",
    },
    "python-flask": {
        "detector": ["app.py"],
        "expose": "5000",
        "cmd": "flask run --host 0.0.0.0 --port 5000",
    },
}


def detect_stack(store: dict[str, str]) -> Optional[str]:
    files = set(Path(p).name for p in store.keys())

    for stack_name, config in STACK_PATTERNS.items():
        if any(detector in files for detector in config["detector"]):
            return stack_name

    return None


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

## 공통 필수 규칙

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
- 각 스테이지는 `AS <name>`으로 명명"""

HUMAN_PROMPT = """다음은 소스코드 디렉토리 구조입니다.

{tree}

{context}

{detect_info}

read_file 도구로 Dockerfile 작성에 필요한 추가 파일을 읽은 뒤, Dockerfile을 생성해주세요."""


class DockerfileGenerator(BaseDockerfileGenerator):
    def __init__(self) -> None:
        self.llm = NovaLLM()
        self.stack_patterns = STACK_PATTERNS

    def _build_stack_info(self) -> str:
        lines = []
        for name, config in self.stack_patterns.items():
            lines.append(f"- {name}: {config['detector']}")
        return "\n".join(lines)

    def _build_detect_info(self, stack: Optional[str]) -> str:
        if stack is None:
            return (
                "감지된 스택 없음. 파일을 직접 분석하여 적절한 Dockerfile을 생성하세요."
            )

        config = self.stack_patterns[stack]
        lines = [f"감지된 스택: {stack}", f"- EXPOSE: {config['expose']}"]

        if stack == "node-static":
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

        @tool
        def read_file(path: str) -> str:
            """소스코드 파일의 내용을 읽습니다. path는 트리에 표시된 경로를 그대로 사용하세요."""
            content = store.get(path)
            if content is None:
                return f"[오류] 파일을 찾을 수 없습니다: {path}"
            logger.info(f"[DockerfileGenerator] read_file: {path}")
            return content

        llm_with_tools = self.llm.client.bind_tools([read_file])
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

        dockerfile = await self._run_agent(llm_with_tools, read_file, messages)
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

    async def _run_agent(
        self,
        llm_with_tools,
        read_file_tool: Callable,
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
                result = read_file_tool.invoke(tc["args"])
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
        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
        content = re.sub(r"```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"^\s*#.*\n?", "", content, flags=re.MULTILINE)
        content = re.sub(
            r"(COPY\s+\S+)\.([ \t]*/|[ \t]*$)", r"\1 .\2", content, flags=re.MULTILINE
        )
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = _merge_run_layers(content)
        content = _merge_env_layers(content)
        return content.strip()
