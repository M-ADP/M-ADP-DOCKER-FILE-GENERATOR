import logging

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from src.infra.llm.docker_hub_verifier import DockerHubVerifier
from src.infra.llm.dockerfile_processing.paths import _resolve_store_path
from src.infra.llm.dockerfile_processing.paths import _build_tree_from_store

logger = logging.getLogger(__name__)


class ReadFileInput(BaseModel):
    path: str = Field(description="소스코드 파일의 경로")


class ListTreeInput(BaseModel):
    path: str = Field(default="", description="조회할 디렉토리 경로. 루트는 빈 문자열")
    max_depth: int = Field(default=3, ge=1, le=8, description="조회할 최대 깊이")


class VerifyDockerImageInput(BaseModel):
    image_with_tag: str = Field(description="검증할 이미지 태그 (예: node:22-alpine)")


class SearchDockerImageInput(BaseModel):
    language: str = Field(description="검색할 프로그래밍 언어 이름")


class DockerfileAgentTools:
    def __init__(self, store: dict[str, str], verifier: DockerHubVerifier) -> None:
        self._store = store
        self._verifier = verifier

    def build(self) -> list:
        store = self._store
        verifier = self._verifier

        @tool(args_schema=ReadFileInput)
        def read_file(path: str) -> str:
            """소스코드 파일의 내용을 읽습니다."""
            resolved = _resolve_store_path(store, path)
            content = store.get(resolved) if resolved else None
            if content is None:
                return f"[오류] 파일을 찾을 수 없습니다: {path}"
            logger.info(f"[AgentTools] read_file: {resolved}")
            return content

        @tool(args_schema=ListTreeInput)
        def list_tree(path: str = "", max_depth: int = 3) -> str:
            """소스코드 디렉토리 구조를 조회합니다."""
            normalized = "" if path in ("", ".") else path
            logger.info(f"[AgentTools] list_tree: path={normalized or '.'}, depth={max_depth}")
            return _build_tree_from_store(store, normalized, max_depth)

        @tool(args_schema=VerifyDockerImageInput)
        async def verify_docker_image(image_with_tag: str) -> str:
            """Docker Hub에서 베이스 이미지 태그가 존재하는지 검증합니다."""
            parts = image_with_tag.split(":")
            if len(parts) != 2:
                return "ERROR: 'image:tag' 형식으로 입력하세요 (예: node:22-alpine)"
            image, tag = parts
            result = await verifier.verify(image, tag)
            if result["exists"]:
                return f"EXISTS: {image_with_tag} - 크기: {result.get('size', 'unknown')} bytes"
            error = result.get("error", "unknown")
            suggestion = result.get("suggestion", "")
            return f"NOT_FOUND: {image_with_tag} - {error}. {suggestion}"

        @tool(args_schema=SearchDockerImageInput)
        def search_docker_image(language: str) -> str:
            """프로그래밍 언어에 대한 권장 베이스 이미지를 검색합니다."""
            suggested = verifier.suggest_image(language)
            if suggested:
                return f"SUGGESTED: {suggested} — verify_docker_image('{suggested}')로 검증하세요."
            detected = verifier.detect_language_from_files(set(store.keys()))
            if detected:
                alt = verifier.suggest_image(detected)
                if alt:
                    return f"SUGGESTED: {alt} — 파일 분석 감지: {detected}"
            return f"NOT_FOUND: {language} — 지원 언어: python, node, java, go, rust, ruby, php 등"

        return [read_file, list_tree, verify_docker_image, search_docker_image]
