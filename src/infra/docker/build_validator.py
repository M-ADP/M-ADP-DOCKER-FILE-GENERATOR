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
    "cannot find",
    "no such file or directory",
    "exit code",
    "npm err",
    "err_",        # ERR_PNPM_NO_PKG_MANIFEST 등 패키지 매니저 에러 코드
    "error -",
)
# 빌드 중 발생하는 런타임 노이즈 — Dockerfile 구조 오류가 아니므로 제외
_ERROR_KEYWORDS_EXCLUDE = (
    "permission denied",  # npm/pnpm 캐시 디렉토리 접근 실패 등 노이즈 — Dockerfile 구조 오류 아님
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

    @staticmethod
    def _build_target(dockerfile_content: str) -> str | None:
        """Dockerfile에 AS builder 스테이지가 있으면 'builder', 없으면 None."""
        for line in dockerfile_content.splitlines():
            if re.match(r"FROM\s+\S+\s+AS\s+builder\b", line.strip(), re.IGNORECASE):
                return "builder"
        return None

    async def _run_build(self, context_dir: str) -> BuildResult:
        start = time.monotonic()
        logger.info(f"[BuildValidator] buildctl addr={_BUILDKIT_ADDR}")

        dockerfile_content = ""
        try:
            dockerfile_content = (Path(context_dir) / "Dockerfile").read_text()
            logger.info("[BuildValidator] Dockerfile to validate:\n%s", dockerfile_content)
        except Exception:
            pass

        target = self._build_target(dockerfile_content)
        logger.info("[BuildValidator] build target=%s", target)

        cmd = [
            "buildctl", "--addr", _BUILDKIT_ADDR,
            "build",
            "--frontend", "dockerfile.v0",
            "--local", f"context={context_dir}",
            "--local", f"dockerfile={context_dir}",
            "--progress", "plain",
        ]
        if target:
            cmd += ["--opt", f"target={target}"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
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
        primary: list[str] = []   # BuildKit #N ERROR: 라인 (가장 직접적인 원인)
        secondary: list[str] = [] # 키워드 매칭 보조 에러

        for line in stderr.splitlines():
            stripped = line.strip()
            low = stripped.lower()

            # BuildKit 진행 메시지 제외
            if re.match(r"#\d+\s+(done|exporting|sending|resolving|pulling|mounting|unpacking)", low):
                continue

            # 노이즈 제외 — 런타임 부산물
            if any(kw in low for kw in _ERROR_KEYWORDS_EXCLUDE):
                continue

            # #N ERROR: ... 패턴 — 실제 빌드 실패 원인
            if re.match(r"#\d+\s+error:", low):
                clean = re.sub(r"^#\d+\s+", "", stripped)
                primary.append(clean)
                continue

            if any(kw in low for kw in _ERROR_KEYWORDS):
                clean = re.sub(r"^#\d+\s+", "", stripped)
                secondary.append(clean)

        errors = (primary + secondary)[:10]
        logger.debug("[BuildValidator] parsed errors (primary=%d, secondary=%d): %s",
                     len(primary), len(secondary), errors)
        return errors
