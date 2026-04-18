import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

logger = logging.getLogger(__name__)

_BUILD_TIMEOUT = 180
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
        if not await self._is_nerdctl_available():
            logger.info("[BuildValidator] nerdctl 없음, 빌드 검증 건너뜁니다.")
            return BuildResult(success=True)
        with TemporaryDirectory() as tmpdir:
            self._write_store(tmpdir, store)
            dockerfile_path = self._write_dockerfile(tmpdir, dockerfile)
            return await self._run_build(tmpdir, dockerfile_path)

    @staticmethod
    async def _is_nerdctl_available() -> bool:
        try:
            # nerdctl info는 containerd 소켓까지 실제로 확인함
            proc = await asyncio.create_subprocess_exec(
                "nerdctl", "info",
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
    def _write_dockerfile(tmpdir: str, dockerfile: str) -> Path:
        path = Path(tmpdir) / "Dockerfile"
        path.write_text(dockerfile, encoding="utf-8")
        return path

    async def _run_build(self, context_dir: str, dockerfile_path: Path) -> BuildResult:
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "nerdctl", "build",
            "--file", str(dockerfile_path),
            "--target", "builder",
            "--output", "type=cacheonly",
            "--progress", "plain",
            "--no-cache",
            context_dir,
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
