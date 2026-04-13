import io
import logging
import tarfile
from pathlib import Path

from src.common.const.llm import MAX_FILE_CHARS
from src.core.exceptions import InvalidArchiveError

logger = logging.getLogger(__name__)


class SourceCollector:

    def extract_store(self, tar_bytes: bytes) -> dict[str, str]:
        """tar.gz에서 텍스트 파일을 추출해 {path: content} dict 반환.

        UTF-8 디코딩에 실패하는 파일(바이너리)은 자동 제외.
        단일 파일이 MAX_FILE_CHARS 초과 시 잘라냄.
        """
        try:
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
                store: dict[str, str] = {}

                for member in tar.getmembers():
                    if not member.isfile():
                        continue

                    name = member.name.lstrip("./")
                    parts = Path(name).parts

                    # 숨김 디렉토리 제외 (.git 등)
                    if any(p.startswith(".") for p in parts[:-1]):
                        continue

                    f = tar.extractfile(member)
                    if f is None:
                        continue

                    raw = f.read()
                    try:
                        content = raw.decode("utf-8", errors="strict")
                    except (UnicodeDecodeError, ValueError):
                        continue  # 바이너리 파일 제외

                    if len(content) > MAX_FILE_CHARS:
                        content = content[:MAX_FILE_CHARS] + "\n... (truncated)"

                    store[name] = content

                logger.info(f"[SourceCollector] extracted {len(store)} files")
                return store

        except tarfile.TarError as e:
            raise InvalidArchiveError() from e

    def build_tree(self, store: dict[str, str]) -> str:
        """파일 경로 목록을 트리 형식 문자열로 변환.

        디렉토리를 파일보다 먼저 표시하며, 각 레벨은 알파벳 순 정렬.
        """
        tree: dict = {}
        for path in sorted(store.keys()):
            parts = Path(path).parts
            node = tree
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node.setdefault(parts[-1], None)  # None = leaf(파일)

        lines: list[str] = []

        def _render(node: dict, prefix: str = "") -> None:
            # 디렉토리 먼저, 그 다음 파일 (각각 알파벳 순)
            items = sorted(node.items(), key=lambda x: (x[1] is None, x[0]))
            for i, (name, children) in enumerate(items):
                is_last = i == len(items) - 1
                connector = "└── " if is_last else "├── "
                display = f"{name}/" if children is not None else name
                lines.append(f"{prefix}{connector}{display}")
                if children is not None:
                    extension = "    " if is_last else "│   "
                    _render(children, prefix + extension)

        _render(tree)
        return "\n".join(lines)
