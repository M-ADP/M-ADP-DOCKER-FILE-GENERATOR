import logging
from typing import Optional

from langchain_core.messages import HumanMessage

from src.core.spec.models import BuildSpec
from src.infra.docker.build_validator import BuildResult, DockerBuildValidator
from src.infra.linting.hadolint import HadolintIssue, HadolintValidator
from src.infra.llm.agent.loop import AgentLoop
from src.infra.llm.dockerfile_processing.stack_handlers import (
    apply_store_fixers,
    collect_stack_issues,
)
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
                result = apply_store_fixers(result, store, stack)

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
        if "site-packages" in error:
            return (
                f"이전 Dockerfile 수정 필요:\n{error}\n\n"
                "힌트: runner stage에 다음 세 줄을 반드시 추가하세요:\n"
                "  COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages\n"
                "  COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn\n"
                "  COPY --from=builder /app ./\n"
                "Dockerfile만 다시 생성하세요."
            )

        if "pip install 전에" in error:
            return (
                f"이전 Dockerfile 수정 필요:\n{error}\n\n"
                "힌트: pip install RUN 명령 바로 앞에 `COPY requirements.txt ./` 를 추가하세요.\n"
                "Dockerfile만 다시 생성하세요."
            )

        if "COPY에서 누락" in error:
            hint = (
                f"소스 파일이 `{project_root}` 서브디렉토리 아래에 있습니다. "
                f"COPY 경로에 반드시 서브디렉토리 prefix를 포함하세요.\n"
                f"  올바른 예: COPY {project_root}requirements.txt .\n"
                f"  또는:     COPY {project_root} .\n"
                f"  잘못된 예: COPY requirements.txt ."
            ) if project_root else "list_tree로 파일 구조를 다시 확인하세요."
            return f"이전 Dockerfile 수정 필요:\n{error}\n\n힌트: {hint}\nDockerfile만 다시 생성하세요."

        if "permission denied" in error.lower():
            return (
                f"이전 Dockerfile 빌드 중 권한 오류:\n{error}\n\n"
                "힌트: USER <non-root> 전환 이후에 RUN mkdir, adduser, useradd 등을 실행하면 권한 오류가 발생합니다.\n"
                "올바른 순서:\n"
                "  1. USER 전환 전에 root 권한으로 사용자/그룹 생성 및 디렉토리 준비\n"
                "  2. COPY --chown=user:group 으로 소유권 설정\n"
                "  3. 마지막에 USER <non-root> 로 전환\n"
                "Alpine 예시: RUN addgroup -S appgroup && adduser -S appuser -G appgroup\n"
                "Debian 예시: RUN groupadd -r appgroup && useradd -r -g appgroup appuser\n"
                "Dockerfile만 다시 생성하세요."
            )

        if "corepack prepare" in error or "corepack" in error.lower() and "--destination" in error:
            return (
                f"이전 Dockerfile 수정 필요:\n{error}\n\n"
                "힌트: `corepack prepare --destination`은 존재하지 않는 플래그입니다.\n"
                "올바른 pnpm 패턴:\n"
                "  `RUN corepack enable && pnpm install --frozen-lockfile`\n"
                "  또는: `RUN npm install -g pnpm && pnpm install --frozen-lockfile`\n"
                "Dockerfile만 다시 생성하세요."
            )

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

        if "mainclass" in error.lower() or ("bootjar" in error.lower() and "main class" in error.lower()):
            return (
                f"이전 Dockerfile 빌드 실패 — Gradle main class 탐지 실패:\n{error}\n\n"
                "힌트: src/ 디렉토리가 COPY되지 않아 Gradle이 main class를 찾지 못합니다.\n"
                "올바른 Java/Spring 패턴:\n"
                "  FROM gradle:8-jdk17 AS builder\n"
                "  WORKDIR /app\n"
                "  COPY . .\n"
                "  RUN gradle clean bootJar --no-daemon -x test\n"
                "  FROM eclipse-temurin:17-jre-alpine\n"
                "  COPY --from=builder /app/build/libs/*.jar /app/app.jar\n"
                "  CMD [\"java\", \"-jar\", \"/app/app.jar\"]\n"
                "Dockerfile만 다시 생성하세요."
            )

        if ("exit code: 126" in error or "exit code 126" in error) and "gradlew" in error.lower():
            return (
                f"이전 Dockerfile 빌드 중 gradlew 실행 권한 오류 (exit code 126):\n{error}\n\n"
                "힌트: tar에서 복사된 gradlew는 실행 권한이 없습니다. 반드시 chmod를 추가하세요:\n"
                "  RUN chmod +x ./gradlew && ./gradlew clean build --no-daemon\n"
                "Dockerfile만 다시 생성하세요."
            )

        if "빌드 실패" in error:
            return (
                f"이전 Dockerfile이 실제 빌드에 실패했습니다:\n{error}\n"
                "오류 원인을 분석하여 의존성 설치 명령어와 COPY 경로를 수정하세요."
            )

        return f"이전 Dockerfile에 문제가 있습니다:\n{error}\n\nDockerfile을 다시 생성하세요."
