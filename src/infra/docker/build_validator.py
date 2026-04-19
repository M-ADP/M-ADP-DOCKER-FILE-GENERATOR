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
    "permission denied",        # npm/pnpm 캐시 디렉토리 접근 실패 등 노이즈
    "unexpected end of json",   # 빈 placeholder package.json 파싱 실패 — context 한계
    "json parse error",
    "is not valid json",
    "broken_lockfile",          # 잘린 lockfile — collector MAX_FILE_CHARS 한계
    "outdated_lockfile",        # lockfile ↔ package.json 불일치 — context 한계
    # "frozen-lockfile" 제거 — 이 키워드를 포함한 에러 라인을 필터링하면
    # returncode=1인데 errors=[] 상황이 되어 LLM에 빈 피드백이 전달됨
    "lockfile is not up-to-date",
    "your lockfile needs to be updated",  # yarn frozen-lockfile context 한계
)

# lockfile 이름 → 최소 유효 placeholder 내용
_LOCKFILE_PLACEHOLDERS: dict[str, str] = {
    "pnpm-lock.yaml":    "lockfileVersion: '9.0'\n",
    "package-lock.json": '{"name":"","lockfileVersion":3,"packages":{}}\n',
    "yarn.lock":         "# yarn lockfile v1\n",
    "Cargo.lock":        "version = 3\n",
    "poetry.lock":       '[metadata]\nlock-version = "2.0"\npython-versions = "*"\ncontent-hash = ""\n',
    "composer.lock":     '{"packages":[],"packages-dev":[]}\n',
    "go.sum":            "",
    "Gemfile.lock":      "",
}


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
            self._write_copy_placeholders(tmpdir, dockerfile)
            self._sanitize_lockfiles(tmpdir)
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
    def _sanitize_lockfiles(tmpdir: str) -> None:
        """잘린(truncated) lockfile을 최소 유효 placeholder로 교체.

        collector.py의 MAX_FILE_CHARS 제한으로 lockfile이 잘리면 pnpm/yarn/cargo 등이
        broken lockfile 에러를 내며 Dockerfile 구조와 무관한 빌드 실패가 발생한다.
        """
        tmp = Path(tmpdir)
        for name, placeholder in _LOCKFILE_PLACEHOLDERS.items():
            lf = tmp / name
            if not lf.exists():
                continue
            try:
                content = lf.read_text(encoding="utf-8")
                if "(truncated)" in content:
                    lf.write_text(placeholder, encoding="utf-8")
                    logger.info("[BuildValidator] truncated lockfile replaced with placeholder: %s", name)
            except OSError as e:
                logger.debug("[BuildValidator] lockfile sanitize 실패: %s — %s", name, e)

    @staticmethod
    def _write_copy_placeholders(tmpdir: str, dockerfile: str) -> None:
        """Dockerfile COPY 소스 중 컨텍스트에 없는 파일을 빈 placeholder로 생성.

        store에 없는 .yarnrc, .npmrc 등 config 파일 때문에 COPY가 실패해
        구조적으로 올바른 Dockerfile이 오탐지되는 것을 방지한다.
        """
        tmp = Path(tmpdir)
        for line in dockerfile.splitlines():
            stripped = line.strip()
            if not stripped.startswith("COPY ") or "--from=" in stripped:
                continue
            parts = stripped.split()
            # COPY [--opt] src... dest — 마지막이 dest, 나머지가 src
            srcs = [p for p in parts[1:-1] if not p.startswith("--")]
            for src in srcs:
                if "*" in src or "?" in src:
                    continue
                target = tmp / src
                if target.exists():
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if src.endswith("/") or "." not in target.name:
                        target.mkdir(exist_ok=True)
                        logger.debug("[BuildValidator] placeholder dir: %s", src)
                    else:
                        target.write_text("", encoding="utf-8")
                        logger.debug("[BuildValidator] placeholder file: %s", src)
                except OSError as e:
                    logger.debug("[BuildValidator] placeholder 생성 실패: %s — %s", src, e)

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

        stderr_text = stderr.decode("utf-8", errors="replace")
        errors = self._parse_errors(stderr_text)

        if not errors:
            # 모든 에러가 context 한계 노이즈로 필터링된 경우 — Dockerfile 구조 문제 아님
            logger.info(
                "[BuildValidator] returncode=%d but all errors filtered (context limitation). "
                "Treating as success. stderr tail: %s",
                proc.returncode,
                stderr_text[-300:].strip(),
            )
            return BuildResult(success=True, build_time_ms=elapsed_ms)

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
