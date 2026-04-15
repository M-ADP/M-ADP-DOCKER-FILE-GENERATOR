import io
import tarfile
from pathlib import Path

from src.core.exceptions import AppException


class SecurityViolationError(AppException):
    status_code = 400
    message = "보안 위반 감지"


class TarBombError(AppException):
    status_code = 400
    message = "압축 파일 크기가 허용치를 초과했습니다."


class PathTraversalError(AppException):
    status_code = 400
    message = "경로 탈출 시도가 감지되었습니다."


class FileCountExceededError(AppException):
    status_code = 400
    message = "파일 수가 허용치를 초과했습니다."


class DangerousDockerfileError(AppException):
    status_code = 400
    message = "위험한 Dockerfile 패턴이 감지되었습니다."


class TarSecurityGuard:
    MAX_EXTRACT_SIZE = 500 * 1024 * 1024
    MAX_FILE_COUNT = 10_000
    MAX_SINGLE_FILE_SIZE = 100 * 1024 * 1024

    def validate(self, tar_bytes: bytes) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
                self._check_file_count(tar)
                self._check_paths(tar)
                self._check_total_size(tar)
        except AppException:
            raise
        except tarfile.TarError as e:
            raise SecurityViolationError(str(e)) from e

    def _check_file_count(self, tar: tarfile.TarFile) -> None:
        members = tar.getmembers()
        if len(members) > self.MAX_FILE_COUNT:
            raise FileCountExceededError(f"파일 수: {len(members)}")

    def _check_paths(self, tar: tarfile.TarFile) -> None:
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                continue

            normalized = str(Path(member.name).resolve())
            if normalized.startswith("..") or member.name.startswith("/"):
                raise PathTraversalError(f"경로: {member.name}")

    def _check_total_size(self, tar: tarfile.TarFile) -> None:
        total_size = 0
        for member in tar.getmembers():
            if member.isfile():
                if member.size > self.MAX_SINGLE_FILE_SIZE:
                    raise TarBombError(f"단일 파일 크기 초과: {member.name}")
                total_size += member.size
                if total_size > self.MAX_EXTRACT_SIZE:
                    raise TarBombError(f"총 크기 초과: {total_size} bytes")


class DockerfileSecurityGuard:
    DANGEROUS_PATTERNS = [
        (r"RUN\s+.*curl.*\|.*sh", "원격 스크립트 실행"),
        (r"RUN\s+.*wget.*\|.*sh", "원격 스크립트 실행"),
        (r"RUN\s+.*\$[A-Z_]+", "환경변수 출력"),
        (r"RUN\s+.*cat\s+/etc", "민감 파일 읽기"),
        (r"ADD\s+https?://", "원격 파일 직접 추가"),
        (r"RUN\s+.*base64", "base64 인코딩 명령"),
    ]

    def validate(self, dockerfile: str) -> None:
        import re

        for pattern, reason in self.DANGEROUS_PATTERNS:
            if re.search(pattern, dockerfile, re.IGNORECASE):
                raise DangerousDockerfileError(f"위험 패턴: {reason}")


class SourceSecurityGuard:
    SENSITIVE_PATTERNS = [
        r"IGNORE\s+ALL\s+PREVIOUS\s+INSTRUCTIONS",
        r"SYSTEM\s*PROMPT",
        r"return\s+exactly\s+this\s+Dockerfile",
    ]

    def validate(self, source: str, path: str) -> None:
        import re

        for pattern in self.SENSITIVE_PATTERNS:
            if re.search(pattern, source, re.IGNORECASE):
                raise SecurityViolationError(f"민감 패턴 감지: {path}")


class CompositeSecurityGuard:
    def __init__(self) -> None:
        self.tar_guard = TarSecurityGuard()
        self.dockerfile_guard = DockerfileSecurityGuard()
        self.source_guard = SourceSecurityGuard()

    def validate_tar(self, tar_bytes: bytes) -> None:
        self.tar_guard.validate(tar_bytes)

    def validate_source(self, source: str, path: str) -> None:
        self.source_guard.validate(source, path)

    def validate_dockerfile(self, dockerfile: str) -> None:
        self.dockerfile_guard.validate(dockerfile)
