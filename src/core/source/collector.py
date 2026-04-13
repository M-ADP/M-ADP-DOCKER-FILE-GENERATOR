import io
import logging
import os
import tarfile
from pathlib import Path
from typing import List

from src.common.const.llm import MAX_CONTEXT_CHARS, MAX_FILE_CHARS
from src.core.exceptions import InvalidArchiveError
from src.core.source.model import SourceFile

logger = logging.getLogger(__name__)

_LANGUAGE_DETECTION_FILES = frozenset({
    "package.json", "pom.xml", "build.gradle", "build.gradle.kts",
    "requirements.txt", "go.mod", "Cargo.toml", "composer.json",
    "Pipfile", "setup.py", "setup.cfg", "pyproject.toml",
})

_ENTRY_POINT_FILES = frozenset({
    "main.py", "app.py", "server.py", "wsgi.py", "asgi.py",
    "index.js", "app.js", "server.js",
    "index.ts", "app.ts", "main.ts",
    "main.go",
    "Main.java", "Application.java",
    "main.rs",
    "manage.py",
})

_CONFIG_FILES = frozenset({
    "application.yml", "application.yaml",
    ".env.example", ".env.sample",
    "config.yml", "config.yaml",
    "nginx.conf",
})

_SOURCE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".kt", ".scala",
    ".go",
    ".rs",
    ".rb", ".php",
    ".cs", ".cpp", ".c", ".h", ".hpp",
    ".yml", ".yaml", ".json", ".toml", ".xml", ".gradle",
    ".sh", ".bash",
})


class SourceCollector:

    def collect(self, tar_bytes: bytes) -> List[SourceFile]:
        files = self._extract(tar_bytes)
        return self._trim(files)

    def _extract(self, tar_bytes: bytes) -> List[SourceFile]:
        try:
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
                files: List[SourceFile] = []

                for member in tar.getmembers():
                    if not member.isfile():
                        continue

                    name = member.name.lstrip("./")
                    parts = Path(name).parts

                    if any(p.startswith(".") for p in parts[:-1]):
                        continue

                    priority = self._priority(name)
                    if priority == 99:
                        continue

                    f = tar.extractfile(member)
                    if f is None:
                        continue

                    try:
                        content = f.read().decode("utf-8", errors="replace")
                    except Exception:
                        continue

                    if len(content) > MAX_FILE_CHARS:
                        content = content[:MAX_FILE_CHARS] + "\n... (truncated)"

                    files.append(SourceFile(path=name, content=content, priority=priority))

                logger.info(f"[SourceCollector] extracted {len(files)} files")
                return files

        except tarfile.TarError as e:
            raise InvalidArchiveError() from e

    def _trim(self, files: List[SourceFile]) -> List[SourceFile]:
        sorted_files = sorted(files, key=lambda f: (f.priority, f.path))

        total_chars = 0
        result: List[SourceFile] = []

        for f in sorted_files:
            file_chars = len(f.content) + len(f.path) + 20
            if total_chars + file_chars > MAX_CONTEXT_CHARS and f.priority >= 4:
                logger.debug(f"[SourceCollector] skip {f.path} (context limit)")
                continue
            total_chars += file_chars
            result.append(f)

        logger.info(f"[SourceCollector] context={total_chars} chars, files={len(result)}")
        return result

    @staticmethod
    def _priority(filepath: str) -> int:
        name = os.path.basename(filepath)
        if name in _LANGUAGE_DETECTION_FILES:
            return 1
        if name in _ENTRY_POINT_FILES:
            return 2
        if name in _CONFIG_FILES:
            return 3
        if os.path.splitext(name)[1].lower() in _SOURCE_EXTENSIONS:
            return 4
        return 99
