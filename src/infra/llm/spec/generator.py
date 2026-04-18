import json
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from src.core.manifest.models import ManifestInfo
from src.core.spec.models import BuildSpec
from src.infra.llm.nova import NovaLLM
from src.infra.llm.spec.prompts import SPEC_HUMAN_PROMPT, SPEC_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class SpecGenerator:
    def __init__(self, llm: NovaLLM) -> None:
        self._llm = llm

    async def generate(
        self,
        manifest: ManifestInfo,
        tree: str,
        stack_hint: Optional[str],
        project_root: str,
    ) -> BuildSpec:
        messages = [
            SystemMessage(content=SPEC_SYSTEM_PROMPT),
            HumanMessage(
                content=SPEC_HUMAN_PROMPT.format(
                    tree=tree,
                    manifest_summary=self._format_manifest(manifest),
                    stack_hint=stack_hint or "unknown",
                    project_root=project_root or "(루트)",
                )
            ),
        ]

        response = await self._llm.client.ainvoke(messages)
        return self._parse_response(response.content)

    def _parse_response(self, content: str) -> BuildSpec:
        raw = content if isinstance(content, str) else str(content)
        raw = raw.strip()

        # 마크다운 코드블록 제거
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        try:
            data = json.loads(raw)
            return BuildSpec.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.error(f"[SpecGenerator] parse failed: {e}\nraw={raw[:200]}")
            raise ValueError(f"SpecGenerator 응답 파싱 실패: {e}") from e

    @staticmethod
    def _format_manifest(manifest: ManifestInfo) -> str:
        lines = [
            f"언어: {manifest.language}",
            f"런타임 버전: {manifest.runtime_version or '미감지'}",
            f"패키지 매니저: {manifest.pkg_manager}"
            + (f" {manifest.pkg_manager_version}" if manifest.pkg_manager_version else ""),
            f"엔트리포인트: {manifest.entry_point or '미감지'}",
            f"감지된 포트: {manifest.detected_port or '미감지'}",
        ]

        if manifest.dependencies:
            top_deps = list(manifest.dependencies.items())[:20]
            lines.append("주요 의존성: " + ", ".join(f"{k}@{v}" for k, v in top_deps))

        if manifest.dev_dependencies:
            top_dev = list(manifest.dev_dependencies.items())[:10]
            lines.append("개발 의존성: " + ", ".join(f"{k}@{v}" for k, v in top_dev))

        if manifest.scripts:
            lines.append("스크립트: " + ", ".join(manifest.scripts.keys()))

        if manifest.raw_deps:
            lines.append("직접 의존성: " + ", ".join(manifest.raw_deps[:20]))

        if manifest.extra:
            for k, v in manifest.extra.items():
                lines.append(f"{k}: {v}")

        return "\n".join(lines)
