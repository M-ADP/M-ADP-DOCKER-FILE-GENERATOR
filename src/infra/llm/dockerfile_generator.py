import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src.core.generators import BaseDockerfileGenerator
from src.core.llm import LLM
from src.core.manifest.models import ManifestInfo
from src.core.params.analyzer import Analyzer
from src.core.params.rule_engine import RuleEngine
from src.infra.docker.build_validator import DockerBuildValidator
from src.infra.llm.agent.loop import AgentLoop
from src.infra.llm.agent.retry import RetryLoop
from src.infra.llm.agent.tools import DockerfileAgentTools
from src.infra.llm.docker_hub_verifier import DockerHubVerifier
from src.infra.llm.dockerfile_processing.constants import STACK_PATTERNS
from src.infra.llm.dockerfile_processing.cleaner import inject_vault_entrypoint
from src.infra.llm.dockerfile_processing.ignore import (
    _remove_missing_optional_copy_sources,
    _remove_unwanted_copy_sources,
    generate_dockerignore,
)
from src.infra.llm.dockerfile_processing.prompts import HUMAN_PROMPT, SYSTEM_PROMPT
from src.infra.llm.dockerfile_processing.stack_handlers import (
    apply_stack_fixers,
    apply_store_fixers,
)
from src.infra.llm.template.selector import select
from src.infra.linting.hadolint import HadolintValidator

logger = logging.getLogger(__name__)

_analyzer     = Analyzer()
_rule_engine  = RuleEngine()


class DockerfileGenerator(BaseDockerfileGenerator):
    def __init__(
        self,
        llm: LLM,
        hadolint: HadolintValidator,
        build_validator: DockerBuildValidator,
        verifier: DockerHubVerifier,
    ) -> None:
        self._llm             = llm
        self._hadolint        = hadolint
        self._build_validator = build_validator
        self._verifier        = verifier

    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        manifest: ManifestInfo,
        feedback: str = "",
        prev_dockerfile: str = "",
    ) -> tuple[str, str, int]:

        # ── 1. 결정론적 분석 ──────────────────────────────────────────────────
        detected = _analyzer.analyze(store, manifest)
        params   = _rule_engine.resolve(detected, store)

        # ── 2. 템플릿 경로 (알려진 스택 + unknown 없음) ───────────────────────
        render_fn = select(params)
        if render_fn is not None and not params.has_unknowns():
            try:
                dockerfile = render_fn(params)
                dockerfile = _remove_unwanted_copy_sources(dockerfile)
                dockerfile = _remove_missing_optional_copy_sources(dockerfile, store)
                dockerfile = inject_vault_entrypoint(dockerfile)
                dockerignore = generate_dockerignore(store, detected.framework, dockerfile)
                port = params.port.value
                logger.info(
                    "[DockerfileGenerator] template path: framework=%s standalone=%s",
                    detected.framework, detected.standalone,
                )
                return dockerfile.strip(), dockerignore.strip(), port
            except Exception as exc:
                logger.warning(
                    "[DockerfileGenerator] template render failed (%s), falling back to LLM", exc
                )

        # ── 3. LLM fallback (미지원 스택 or unknown params) ───────────────────
        stack        = detected.framework
        project_root = detected.project_root
        logger.info(
            "[DockerfileGenerator] LLM path: framework=%s unknowns=%s",
            stack, params.unknown_fields(),
        )

        tools_factory = DockerfileAgentTools(store, self._verifier)
        tools         = tools_factory.build()
        llm_with_tools = self._llm.client.bind_tools(tools)
        agent_loop    = AgentLoop(llm_with_tools)
        retry_loop    = RetryLoop(agent_loop, self._hadolint, self._build_validator)

        # LLM fallback용 spec 생성 (BuildParams에서 직접 조합)
        from src.core.spec.models import BuildSpec
        spec = self._params_to_spec(params, stack, project_root)

        context = self._build_feedback_context(feedback, prev_dockerfile)
        messages = [
            SystemMessage(content=SYSTEM_PROMPT.format(stack_info=self._build_stack_info())),
            HumanMessage(
                content=HUMAN_PROMPT.format(
                    tree=tree,
                    context=context,
                    detect_info=self._format_params(params),
                )
            ),
        ]

        dockerfile = await retry_loop.run(
            messages=messages,
            tools=tools,
            store=store,
            stack=stack,
            project_root=project_root,
            spec=spec,
        )

        dockerfile = _remove_unwanted_copy_sources(dockerfile)
        dockerfile = _remove_missing_optional_copy_sources(dockerfile, store)
        dockerfile = inject_vault_entrypoint(dockerfile)
        dockerignore = generate_dockerignore(store, stack, dockerfile)
        port = self._extract_port(dockerfile, stack)
        return dockerfile, dockerignore, port

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _params_to_spec(params, stack, project_root):
        """LLM fallback용 최소 BuildSpec 생성 (RetryLoop 인터페이스 호환)."""
        from src.core.spec.models import BuildSpec, Stage
        return BuildSpec(
            detected_stack=stack or "unknown",
            stages=[
                Stage(name="builder", base_image=params.base_image.value),
                Stage(name="runner",  base_image=params.runner_image.value,
                      expose_port=params.port.value),
            ],
            pkg_manager=params.detected.package_manager,
            pkg_manager_install_cmd=params.install_cmd.value,
            pkg_manager_build_cmd=params.build_cmd.value,
            project_root=project_root,
        )

    @staticmethod
    def _format_params(params) -> str:
        import json
        d = params.detected
        summary = {
            "framework":     d.framework,
            "runtime":       d.runtime,
            "package_manager": d.package_manager,
            "project_root":  d.project_root,
            "standalone":    d.standalone,
            "install_cmd":   f"{params.install_cmd.value} ({params.install_cmd.confidence})",
            "build_cmd":     f"{params.build_cmd.value} ({params.build_cmd.confidence})",
            "start_cmd":     f"{params.start_cmd.value} ({params.start_cmd.confidence})",
            "port":          f"{params.port.value} ({params.port.confidence})",
        }
        return (
            "[분석된 빌드 파라미터]\n"
            + json.dumps(summary, ensure_ascii=False, indent=2)
            + "\n\n위 파라미터를 기반으로 Dockerfile을 생성하세요."
        )

    @staticmethod
    def _build_stack_info() -> str:
        lines = []
        for name, config in STACK_PATTERNS.items():
            detectors  = config.get("detector", [])
            secondaries = config.get("secondary", [])
            if detectors:
                lines.append(f"- {name}: {detectors} (score: {config.get('score', 0)})")
            elif secondaries:
                lines.append(f"- {name}: {secondaries} (fallback, score: {config.get('score', 0)})")
        return "\n".join(lines)

    @staticmethod
    def _build_feedback_context(feedback: str, prev_dockerfile: str) -> str:
        if not feedback and not prev_dockerfile:
            return ""
        parts = []
        if prev_dockerfile:
            parts.append(f"[이전 Dockerfile]\n```\n{prev_dockerfile.strip()}\n```")
        if feedback:
            parts.append(f"[빌드 오류]\n{feedback.strip()}")
        parts.append("위 오류를 분석하여 수정된 Dockerfile을 생성하세요.")
        return "\n\n".join(parts) + "\n\n"

    @staticmethod
    def _extract_port(dockerfile: str, stack: Optional[str]) -> int:
        m = re.search(r"EXPOSE\s+(\d+)", dockerfile)
        if m:
            return int(m.group(1))
        if stack and stack in STACK_PATTERNS:
            return int(STACK_PATTERNS[stack]["expose"])
        return 8080
