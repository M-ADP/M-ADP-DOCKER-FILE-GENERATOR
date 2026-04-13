import logging
from typing import Callable

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from src.common.config.nova import NovaSettings
from src.core.generators import BaseDockerfileGenerator

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
- Dockerfile 텍스트만 반환 (설명, 마크다운 코드블록 없이)"""

HUMAN_PROMPT = """다음은 소스코드 디렉토리 구조입니다.

{tree}

{context}

read_file 도구로 Dockerfile 작성에 필요한 추가 파일을 읽은 뒤, Dockerfile을 생성해주세요."""


class DockerfileGenerator(BaseDockerfileGenerator):
    def __init__(self) -> None:
        settings = NovaSettings()
        self.llm = ChatBedrockConverse(
            model=settings.bedrock_model_id,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            region_name=settings.bedrock_region,
        )

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

        llm_with_tools = self.llm.bind_tools([read_file])
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=HUMAN_PROMPT.format(tree=tree, context=context)),
        ]

        return await self._run_agent(llm_with_tools, read_file, messages)

    async def _run_agent(
        self,
        llm_with_tools: ChatBedrockConverse,
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

        content = response.content
        return content if isinstance(content, str) else str(content)
