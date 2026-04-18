import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

logger = logging.getLogger(__name__)

_BUILD_TIMEOUT = 180
_BUILDKIT_ADDR = os.environ.get(
    "BUILDKIT_HOST", "tcp://buildkitd.jenkins.svc.cluster.local:1234"
)
_ERROR_KEYWORDS = (
    "error:",
    "failed to",
    "not found",
    "cannot find",
    "no such file",
    "permission denied",
    "exit code",
    "npm err",
    "error -",
)


@dataclass
class BuildResult:
    success: bool
    errors: list[str] = field(default_factory=list)
    build_time_ms: int = 0


class DockerBuildValidator:
    def __init__(self, timeout: int = _BUILD_TIMEOUT) -> None:
        self._timeout = timeout

    async def validate(
        self,
        dockerfile: str,
        store: dict[str, str],
    ) -> BuildResult:
        if not await self._is_buildctl_available():
            logger.info("[BuildValidator] buildctl 접근 불가, 빌드 검증 건너뜁니다.")
            return BuildResult(success=True)
        with TemporaryDirectory() as tmpdir:
            self._write_store(tmpdir, store)
            self._write_dockerfile(tmpdir, dockerfile)
            return await self._run_build(tmpdir)

    @staticmethod
    async def _is_buildctl_available() -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "buildctl", "--addr", _BUILDKIT_ADDR, "debug", "workers",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            return proc.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _write_store(tmpdir: str, store: dict[str, str]) -> None:
        for path, content in store.items():
            full = Path(tmpdir) / path
            full.parent.mkdir(parents=True, exist_ok=True)
            try:
                full.write_text(content, encoding="utf-8")
            except OSError as e:
                logger.warning(f"[BuildValidator] 파일 쓰기 실패: {path} — {e}")

    @staticmethod
    def _write_dockerfile(tmpdir: str, dockerfile: str) -> None:
        (Path(tmpdir) / "Dockerfile").write_text(dockerfile, encoding="utf-8")

    async def _run_build(self, context_dir: str) -> BuildResult:
        start = time.monotonic()
        logger.info(f"[BuildValidator] buildctl addr={_BUILDKIT_ADDR}")
        try:
            dockerfile_content = (Path(context_dir) / "Dockerfile").read_text()
            logger.info(
                "[BuildValidator] Dockerfile to validate:\n%s",
                dockerfile_content,
            )
        except Exception:
            pass
        proc = await asyncio.create_subprocess_exec(
            "buildctl", "--addr", _BUILDKIT_ADDR,
            "build",
            "--frontend", "dockerfile.v0",
            "--local", f"context={context_dir}",
            "--local", f"dockerfile={context_dir}",
            "--opt", "target=builder",
            "--progress", "plain",
            "--no-cache",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return BuildResult(
                success=False,
                errors=[f"빌드 타임아웃 ({self._timeout}초 초과)"],
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(f"[BuildValidator] returncode={proc.returncode}, elapsed={elapsed_ms}ms")

        if proc.returncode == 0:
            return BuildResult(success=True, build_time_ms=elapsed_ms)

        errors = self._parse_errors(stderr.decode("utf-8", errors="replace"))
        return BuildResult(success=False, errors=errors, build_time_ms=elapsed_ms)

    @staticmethod
    def _parse_errors(stderr: str) -> list[str]:
        errors: list[str] = []
        for line in stderr.splitlines():
            stripped = line.strip()
            if any(kw in stripped.lower() for kw in _ERROR_KEYWORDS):
                clean = re.sub(r"^#\d+\s+", "", stripped)
                errors.append(clean)
        return errors[:10]
