import logging
import re

from langchain_core.messages import ToolMessage

from src.infra.llm.dockerfile_processing.cleaner import (
    _ensure_from_first,
    _fix_wildcard_copy,
    _merge_env_layers,
    _merge_run_layers,
    _normalize_continuation_lines,
    _remove_invalid_lines,
    _sanitize_base_images,
)
from src.infra.llm.dockerfile_processing.stack_handlers import apply_stack_fixers
from src.infra.llm.dockerfile_processing.source_validator import (
    _validate_dockerfile_syntax,
)

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10


class AgentLoop:
    def __init__(self, llm_with_tools) -> None:
        self._llm = llm_with_tools

    async def run(self, messages: list, tools: list, stack: str | None = None) -> str:
        tool_map = {t.name: t for t in tools}
        response = None

        for iteration in range(MAX_TOOL_ITERATIONS):
            response = await self._llm.ainvoke(messages)
            logger.info(
                f"[AgentLoop] iteration={iteration + 1}, tool_calls={len(response.tool_calls)}"
            )

            if not response.tool_calls:
                break

            messages.append(response)
            for tc in response.tool_calls:
                result = await self._invoke_tool(tool_map, tc)
                messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))

        if response is None or not response.content:
            raise ValueError("LLM이 빈 응답을 반환했습니다")

        content = response.content if isinstance(response.content, str) else str(response.content)
        return self._clean(content, stack)

    async def _invoke_tool(self, tool_map: dict, tc: dict) -> str:
        tool_name = tc.get("name", "")
        tool_args = tc.get("args", {})
        logger.info(f"[AgentLoop] tool_call: {tool_name}({tool_args})")

        target = tool_map.get(tool_name)
        if target is None:
            return f"[오류] 알 수 없는 도구: {tool_name}"

        try:
            if hasattr(target, "ainvoke"):
                return await target.ainvoke(tool_args)
            return target.invoke(tool_args)
        except Exception as e:
            logger.error(f"[AgentLoop] tool error: {e}")
            return f"[오류] 도구 실행 실패: {e}"

    @staticmethod
    def _clean(content: str, stack: str | None = None) -> str:
        if not content.strip():
            raise ValueError("LLM이 빈 내용을 반환했습니다")

        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
        content = re.sub(r"```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"^\s*#.*\n?", "", content, flags=re.MULTILINE)
        content = re.sub(
            r"(COPY\s+\S+)\.([ \t]*/|[ \t]*$)", r"\1 .\2", content, flags=re.MULTILINE
        )
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = _normalize_continuation_lines(content)
        content = _fix_wildcard_copy(content)
        content = apply_stack_fixers(content, stack)
        content = _merge_run_layers(content)
        content = _merge_env_layers(content)
        content = _sanitize_base_images(content)
        content = _remove_invalid_lines(content)
        content = _ensure_from_first(content)

        result = content.strip()
        if not result:
            raise ValueError("후처리 후 Dockerfile 내용이 비어 있습니다")
        if not result.startswith("FROM"):
            raise ValueError(f"Dockerfile은 FROM으로 시작해야 합니다. 받은 내용: {result[:100]}")

        is_valid, issues = _validate_dockerfile_syntax(result)
        if not is_valid:
            for issue in issues:
                logger.error(f"[AgentLoop] syntax issue: {issue}")
            raise ValueError(f"Dockerfile 문법 오류: {'; '.join(issues[:3])}")

        return result
