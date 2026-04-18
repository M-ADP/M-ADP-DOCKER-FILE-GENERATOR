import asyncio
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class HadolintIssue:
    code: str
    line: int
    message: str
    severity: str


class HadolintValidator:
    _TIMEOUT = 10.0

    async def validate(self, dockerfile: str) -> list[HadolintIssue]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "hadolint",
                "--format", "json",
                "--no-fail",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=dockerfile.encode()),
                timeout=self._TIMEOUT,
            )
            issues = self._parse(stdout.decode())
            errors   = [i for i in issues if i.severity == "error"]
            warnings = [i for i in issues if i.severity == "warning"]
            logger.info(
                f"[Hadolint] issues={len(issues)} (errors={len(errors)}, warnings={len(warnings)})"
                + (f", blocking={[f'{i.code} L{i.line}' for i in errors]}" if errors else "")
            )
            return issues
        except FileNotFoundError:
            logger.warning("[Hadolint] hadolint 바이너리를 찾을 수 없습니다. 검증을 건너뜁니다.")
            return []
        except asyncio.TimeoutError:
            logger.warning("[Hadolint] 타임아웃")
            return []
        except Exception as e:
            logger.error(f"[Hadolint] 오류: {e}")
            return []

    @staticmethod
    def _parse(output: str) -> list[HadolintIssue]:
        try:
            items = json.loads(output)
            return [
                HadolintIssue(
                    code=item.get("code", ""),
                    line=item.get("line", 0),
                    message=item.get("message", ""),
                    severity=item.get("level", "warning"),
                )
                for item in items
                if item.get("level") in ("error", "warning")
            ]
        except (json.JSONDecodeError, KeyError):
            return []
