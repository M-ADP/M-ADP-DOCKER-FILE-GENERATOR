import logging
from typing import Optional

from langchain_core.messages import HumanMessage

from src.core.spec.models import BuildSpec
from src.infra.docker.build_validator import BuildResult, DockerBuildValidator
from src.infra.linting.hadolint import HadolintIssue, HadolintValidator
from src.infra.llm.agent.loop import AgentLoop
from src.infra.llm.dockerfile_processing.stack_handlers import collect_stack_issues
from src.infra.llm.dockerfile_processing.source_validator import (
    _validate_copy_coverage,
    _validate_dockerfile_against_source,
)

logger = logging.getLogger(__name__)

MAX_AGENT_RETRIES = 3


class RetryLoop:
    def __init__(
        self,
        agent_loop: AgentLoop,
        hadolint: HadolintValidator,
        build_validator: DockerBuildValidator,
    ) -> None:
        self._agent_loop = agent_loop
        self._hadolint = hadolint
        self._build_validator = build_validator

    async def run(
        self,
        messages: list,
        tools: list,
        store: dict[str, str],
        stack: Optional[str],
        project_root: str,
        spec: BuildSpec,
    ) -> str:
        last_error: Exception | None = None

        for attempt in range(MAX_AGENT_RETRIES):
            try:
                result = await self._agent_loop.run(messages, tools, stack=stack)

                feedback = self._collect_static_feedback(result, store, stack, project_root)
                if feedback:
                    raise ValueError(f"정적 검증 실패:\n{feedback}")

                hadolint_issues = await self._hadolint.validate(result)
                blocking = [i for i in hadolint_issues if i.severity == "error"]
                if blocking:
                    raise ValueError(
                        "Hadolint 오류:\n"
                        + "\n".join(
                            f"  {i.code} line {i.line}: {i.message}" for i in blocking[:5]
                        )
                    )

                build_result = await self._build_validator.validate(result, store)
                if not build_result.success:
                    raise ValueError(
                        "빌드 실패:\n"
                        + "\n".join(f"  {e}" for e in build_result.errors)
                    )

                return result

            except ValueError as e:
                last_error = e
                logger.warning(
                    f"[RetryLoop] attempt {attempt + 1}/{MAX_AGENT_RETRIES}: {e}"
                )
                messages.append(
                    HumanMessage(content=self._build_retry_message(str(e), project_root))
                )

        raise ValueError(
            f"최대 재시도 초과 ({MAX_AGENT_RETRIES}회). 마지막 오류: {last_error}"
        )

    @staticmethod
    def _collect_static_feedback(
        dockerfile: str,
        store: dict[str, str],
        stack: Optional[str],
        project_root: str,
    ) -> str:
        issues: list[str] = []
        issues.extend(_validate_dockerfile_against_source(dockerfile, store, stack))
        issues.extend(_validate_copy_coverage(dockerfile, store, project_root))
        issues.extend(collect_stack_issues(dockerfile, stack))
        return "\n".join(issues[:5])

    @staticmethod
    def _build_retry_message(error: str, project_root: str) -> str:
        if "정적 검증" in error or "source" in error.lower():
            hint = (
                f"COPY {project_root} . 를 사용해 전체 디렉토리를 복사하세요."
                if project_root
                else "list_tree와 read_file로 파일 구조를 다시 확인하세요."
            )
            return f"이전 Dockerfile 수정 필요:\n{error}\n\n힌트: {hint}\nDockerfile만 다시 생성하세요."

        if "Hadolint" in error:
            return (
                f"이전 Dockerfile에 린트 오류가 있습니다:\n{error}\n"
                "위 규칙을 준수하여 Dockerfile을 다시 생성하세요."
            )

        if "빌드 실패" in error:
            return (
                f"이전 Dockerfile이 실제 빌드에 실패했습니다:\n{error}\n"
                "오류 원인을 분석하여 의존성 설치 명령어와 COPY 경로를 수정하세요."
            )

        return f"이전 Dockerfile에 문제가 있습니다:\n{error}\n\nDockerfile을 다시 생성하세요."
