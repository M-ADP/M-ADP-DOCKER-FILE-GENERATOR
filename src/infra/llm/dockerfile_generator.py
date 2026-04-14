import logging
import re
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from src.core.generators import BaseDockerfileGenerator
from src.infra.llm.nova import NovaLLM

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10

SYSTEM_PROMPT = """당신은 Dockerfile 전문가입니다.
주어진 소스코드를 분석하여 production-ready Dockerfile을 생성하세요.

## 필수: Dockerfile 작성 전 반드시 아래 질문에 답하세요

Dockerfile을 작성하기 전에 read_file 도구로 의존성 파일(package.json, requirements.txt, go.mod, Cargo.toml 등)을 읽고, 다음 세 가지 질문에 순서대로 답하세요.

**Q1. 빌드 결과물은 무엇인가?**
- 빌드 스크립트(scripts.build)가 어떤 명령을 실행하는지 확인
- 결과물이 정적 파일(HTML/CSS/JS 묶음)인가, 실행 가능한 서버 프로세스인가?
- 판단 기준: 빌드 후 `index.html`이 생기면 정적 파일, 실행 가능한 바이너리나 JS 서버 파일이 생기면 서버

**Q2. 런타임에 무엇이 필요한가?**
- 런타임 의존성(dependencies, 비개발용 패키지)이 존재하는가?
- 런타임 의존성이 없으면 → runner 스테이지에 패키지 설치 불필요
- 런타임 의존성이 있으면 → runner 스테이지에 프로덕션 패키지 설치 필요

**Q3. 컨테이너 시작 시 무엇을 실행하는가?**
- 정적 파일이면: 반드시 `node:alpine + serve` 방식으로 서빙
  - 올바른 패턴:
    FROM node:22-alpine AS builder
    WORKDIR /app
    COPY package*.json ./
    RUN npm ci
    COPY . .
    RUN npm run build
    FROM node:22-alpine
    WORKDIR /app
    RUN npm install -g serve
    COPY --from=builder /app/dist ./dist
    EXPOSE 3000
    CMD ["serve", "-s", "dist", "-l", "3000"]
  - nginx 방식 사용 금지: nginx.conf 등 소스에 없는 파일을 COPY할 위험이 있음
- 서버 프로세스이면: 실제 엔트리포인트 파일 경로를 확인 후 CMD 작성
  - package.json의 main/bin 필드 또는 scripts.start 참고
  - CMD에 명시한 파일이 빌드 결과물에 실제로 포함되는지 검증

## 절대 금지: 소스에 없는 파일 COPY
- Dockerfile에 COPY 명령어를 쓰기 전, 해당 파일이 소스 저장소에 실제로 존재하는지 read_file로 확인
- 확인되지 않은 파일(nginx.conf, config.yml 등)을 COPY하면 빌드가 실패하므로 절대 포함하지 말 것

## 필수: 외부 접근 가능하도록 설정
- **반드시 0.0.0.0으로 바인딩**: 서버 애플리케이션은 반드시 `0.0.0.0` 또는 `0.0.0.0:<port>`로 바인딩해야 함
- **절대 localhost/127.0.0.1 사용 금지**: `localhost`, `127.0.0.1`은 컨테이너 내부에서만 접근 가능하므로 외부에서 연결할 수 없음
- **Node.js**: `--host 0.0.0.0` 또는 `HOST=0.0.0.0` 환경변수 설정
- **Python/Flask/FastAPI**: `host="0.0.0.0"` 설정
- **Spring Boot**: `server.address=0.0.0.0` 설정
- **Express.js**: `app.listen(PORT, '0.0.0.0')` 또는 `HOST=0.0.0.0` 환경변수
- **Django**: `python manage.py runserver 0.0.0.0:8000`
- **Go**: `http.ListenAndServe(":8080", handler)` (비어있는 호스트는 0.0.0.0 의미)
- **Rust (Actix)**: `HttpServer::new(...).bind("0.0.0.0:8080")`

## 공통 규칙
- 멀티스테이지 빌드 사용 (빌드 환경 ≠ 런타임 환경)
- Alpine/Slim 경량 이미지 사용
- 레이어 캐시 최적화 (의존성 설치 → 소스 복사 순서)
- WORKDIR /app 고정
- 비루트 사용자 설정
- EXPOSE 포트 명시
- COPY 명령어는 반드시 소스와 목적지 사이에 공백을 포함해야 합니다 (올바른 예: `COPY requirements.txt .`, `COPY . .`, `COPY package*.json ./`)

## 출력 형식
- 응답은 반드시 FROM 명령어로 시작해야 합니다
- 마크다운 코드 블록(```)을 절대 사용하지 마세요
- # 로 시작하는 주석을 절대 포함하지 마세요
- 설명, 주석, 태그 없이 순수한 Dockerfile 명령어만 출력하세요"""

HUMAN_PROMPT = """다음은 소스코드 디렉토리 구조입니다.

{tree}

{context}

read_file 도구로 Dockerfile 작성에 필요한 추가 파일을 읽은 뒤, Dockerfile을 생성해주세요."""


class DockerfileGenerator(BaseDockerfileGenerator):
    def __init__(self) -> None:
        self.llm = NovaLLM()

    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        context: str,
    ) -> tuple[str, int]:
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
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=HUMAN_PROMPT.format(tree=tree, context=context)),
        ]

        dockerfile = await self._run_agent(llm_with_tools, read_file, messages)
        port = self._extract_port(dockerfile)
        return dockerfile, port

    @staticmethod
    def _extract_port(dockerfile: str) -> int:
        match = re.search(r"EXPOSE\s+(\d+)", dockerfile)
        if match:
            return int(match.group(1))
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
        # Remove comment lines (# ...)
        content = re.sub(r"^\s*#.*\n?", "", content, flags=re.MULTILINE)
        # Fix malformed COPY instructions: `COPY src.` or `COPY src./` → `COPY src .` or `COPY src ./`
        content = re.sub(
            r"(COPY\s+\S+)\.([ \t]*/|[ \t]*$)", r"\1 .\2", content, flags=re.MULTILINE
        )
        # Collapse multiple blank lines into one
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()
