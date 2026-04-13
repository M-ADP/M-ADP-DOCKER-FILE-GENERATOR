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

규칙:
- 멀티스테이지 빌드 사용 (빌드 환경 ≠ 런타임 환경)
- Alpine/Slim 경량 이미지 사용
- 레이어 캐시 최적화 (의존성 설치 → 소스 복사 순서)
- WORKDIR /app 고정
- 비루트 사용자 설정
- EXPOSE 포트 명시
- COPY 명령어는 반드시 소스와 목적지 사이에 공백을 포함해야 합니다 (올바른 예: `COPY requirements.txt .`, `COPY . .`, `COPY package*.json ./`)

출력 형식:
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
    ) -> str:
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

        return await self._run_agent(llm_with_tools, read_file, messages)

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

        content = response.content if isinstance(response.content, str) else str(response.content)
        return self._clean(content)

    @staticmethod
    def _clean(content: str) -> str:
        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
        content = re.sub(r"```[a-zA-Z]*\n?", "", content)
        # Remove comment lines (# ...)
        content = re.sub(r"^\s*#.*\n?", "", content, flags=re.MULTILINE)
        # Fix malformed COPY instructions: `COPY src.` or `COPY src./` → `COPY src .` or `COPY src ./`
        content = re.sub(r"(COPY\s+\S+)\.([ \t]*/|[ \t]*$)", r"\1 .\2", content, flags=re.MULTILINE)
        # Collapse multiple blank lines into one
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()
