import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from src.core.generators import BaseDockerfileGenerator
from src.infra.llm.nova import NovaLLM

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10

STACK_PATTERNS = {
    "node-react": {
        "detector": [
            "package.json",
            "vite.config.js",
            "vite.config.ts",
            "next.config.js",
        ],
        "builder_stage": "npm run build",
        "runtime_base": "node:22-alpine",
        "runtime_deps": "RUN npm install serve\nRUN npm ci --only=production",
        "copy_dist": "COPY --from=builder /app/dist ./dist",
        "expose": "3000",
        "cmd": "./node_modules/.bin/serve -s dist -l 3000",
    },
    "node-express": {
        "detector": ["package.json", "server.js", "app.js", "index.js"],
        "builder_stage": None,
        "runtime_base": "node:22-alpine",
        "runtime_deps": "RUN npm ci --only=production",
        "copy_dist": None,
        "expose": "3000",
        "cmd": "node server.js",
    },
    "python-fastapi": {
        "detector": ["requirements.txt", "main.py", "app.py"],
        "builder_stage": None,
        "runtime_base": "python:3.12-slim",
        "runtime_deps": "RUN pip install --no-cache-dir -r requirements.txt",
        "copy_dist": None,
        "expose": "8000",
        "cmd": "uvicorn main:app --host 0.0.0.0 --port 8000",
    },
    "python-flask": {
        "detector": ["requirements.txt", "app.py"],
        "builder_stage": None,
        "runtime_base": "python:3.12-slim",
        "runtime_deps": "RUN pip install --no-cache-dir -r requirements.txt",
        "copy_dist": None,
        "expose": "5000",
        "cmd": "flask run --host 0.0.0.0 --port 5000",
    },
    "static-html": {
        "detector": ["index.html"],
        "builder_stage": None,
        "runtime_base": "nginx:alpine",
        "runtime_deps": None,
        "copy_dist": "COPY --from=builder /app/dist /usr/share/nginx/html",
        "expose": "80",
        "cmd": "nginx -g 'daemon off;'",
    },
}


def detect_stack(store: dict[str, str]) -> Optional[str]:
    files = set(Path(p).name for p in store.keys())

    for stack_name, config in STACK_PATTERNS.items():
        if any(detector in files for detector in config["detector"]):
            return stack_name

    return None


SYSTEM_PROMPT = """당신은 Dockerfile 전문가입니다.
주어진 소스코드를 분석하여 production-ready Dockerfile을 생성하세요.

## 스택 감지 및 템플릿 적용

소스코드에서 스택을 감지하고, 해당하는 검증된 템플릿을 사용하세요.

### 지원 스택
{stack_info}

## 스택별 규칙

1. **Node.js (React, Express 등)**
   - **절대 `npm install -g` 사용 금지** (npm v10 래퍼 스크립트 문제)
   - 로컬 설치: `npm install <package>`
   - 실행: `./node_modules/.bin/<cmd>`

2. **Python (FastAPI, Flask)**
   - `pip install --no-cache-dir` 사용 (이미지 크기 최적화)
   - uvicorn/flask 명령어에 `--host 0.0.0.0` 필수

3. **정적 파일 (HTML/CSS/JS)**
   - nginx:alpine 사용
   - 빌드 결과물을 `/usr/share/nginx/html`에 복사

## 공통 규칙
- 멀티스테이지 빌드 사용 (빌드 환경 ≠ 런타임 환경)
- Alpine/Slim 경량 이미지 사용
- WORKDIR /app 고정
- 비루트 사용자 설정
- EXPOSE 포트 명시
- COPY 명령어는 소스와 목적지 사이에 공백 포함

## 출력 형식
- 응답은 반드시 FROM 명령어로 시작
- 마크다운 코드 블록(```) 사용 금지
- # 로 시작하는 주석 포함 금지
- 설명 없이 순수한 Dockerfile 명령어만 출력"""

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
        if stack:
            config = self.stack_patterns[stack]
            return f"""감지된 스택: {stack}
- EXPOSE: {config["expose"]}
- CMD: {config["cmd"]}

스택 규칙을 반드시 준수하세요."""
        return "스택이 감지되지 않았습니다. 일반적인 규칙을 적용하세요."

    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        context: str,
    ) -> tuple[str, int]:
        stack = detect_stack(store)
        logger.info(f"[DockerfileGenerator] detected stack: {stack}")

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
        return dockerfile, port

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
        return content.strip()
