import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src.core.generators import BaseDockerfileGenerator
from src.core.manifest.models import ManifestInfo
from src.core.spec.models import BuildSpec
from src.core.stack.detector import StackDetector
from src.core.stack.root_detector import ProjectRootDetector
from src.infra.docker.build_validator import DockerBuildValidator
from src.infra.llm.agent.loop import AgentLoop
from src.infra.llm.agent.retry import RetryLoop
from src.infra.llm.agent.tools import DockerfileAgentTools
from src.infra.llm.docker_hub_verifier import DockerHubVerifier
from src.infra.llm.dockerfile_processing.constants import STACK_PATTERNS
from src.infra.llm.dockerfile_processing.ignore import (
    _remove_missing_optional_copy_sources,
    _remove_unwanted_copy_sources,
    generate_dockerignore,
)
from src.infra.llm.dockerfile_processing.prompts import HUMAN_PROMPT, SYSTEM_PROMPT
from src.infra.llm.nova import NovaLLM
from src.infra.llm.spec.generator import SpecGenerator
from src.infra.linting.hadolint import HadolintValidator

logger = logging.getLogger(__name__)


class DockerfileGenerator(BaseDockerfileGenerator):
    def __init__(
        self,
        llm: NovaLLM,
        spec_generator: SpecGenerator,
        hadolint: HadolintValidator,
        build_validator: DockerBuildValidator,
        verifier: DockerHubVerifier,
        stack_detector: StackDetector,
        root_detector: ProjectRootDetector,
    ) -> None:
        self._llm = llm
        self._spec_generator = spec_generator
        self._hadolint = hadolint
        self._build_validator = build_validator
        self._verifier = verifier
        self._stack_detector = stack_detector
        self._root_detector = root_detector

    async def generate(
        self,
        store: dict[str, str],
        tree: str,
        manifest: ManifestInfo,
    ) -> tuple[str, str, int]:
        stack = self._stack_detector.detect(store)
        project_root = self._root_detector.detect(store, stack)
        logger.info(f"[DockerfileGenerator] stack={stack}, project_root='{project_root}'")

        spec = await self._spec_generator.generate(manifest, tree, stack, project_root)
        logger.info(f"[DockerfileGenerator] spec.detected_stack={spec.detected_stack}")

        tools_factory = DockerfileAgentTools(store, self._verifier)
        tools = tools_factory.build()

        llm_with_tools = self._llm.client.bind_tools(tools)
        agent_loop = AgentLoop(llm_with_tools)
        retry_loop = RetryLoop(agent_loop, self._hadolint, self._build_validator)

        messages = [
            SystemMessage(content=SYSTEM_PROMPT.format(stack_info=self._build_stack_info())),
            HumanMessage(
                content=HUMAN_PROMPT.format(
                    tree=tree,
                    context="",
                    detect_info=self._format_spec(spec),
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
        dockerignore = generate_dockerignore(store, stack, dockerfile)
        port = self._extract_port(dockerfile, stack)
        return dockerfile, dockerignore, port

    @staticmethod
    def _build_stack_info() -> str:
        lines = []
        for name, config in STACK_PATTERNS.items():
            detectors = config.get("detector", [])
            secondaries = config.get("secondary", [])
            if detectors:
                lines.append(f"- {name}: {detectors} (score: {config.get('score', 0)})")
            elif secondaries:
                lines.append(f"- {name}: {secondaries} (fallback, score: {config.get('score', 0)})")
        return "\n".join(lines)

    @staticmethod
    def _format_spec(spec: BuildSpec) -> str:
        import json
        return (
            f"[검증된 빌드 스펙]\n"
            f"{json.dumps(spec.model_dump(), ensure_ascii=False, indent=2)}\n\n"
            f"위 스펙을 기반으로 Dockerfile을 생성하세요. "
            f"스펙과 실제 파일이 다르면 read_file/list_tree로 확인 후 실제를 우선하세요."
        )

    @staticmethod
    def _extract_port(dockerfile: str, stack: Optional[str]) -> int:
        m = re.search(r"EXPOSE\s+(\d+)", dockerfile)
        if m:
            return int(m.group(1))
        if stack and stack in STACK_PATTERNS:
            return int(STACK_PATTERNS[stack]["expose"])
        return 8080
