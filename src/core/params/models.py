from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

T = TypeVar("T")


@dataclass
class RuleResult(Generic[T]):
    value: T
    confidence: Literal["certain", "inferred", "unknown"]
    source: str  # "lockfile:pnpm-lock.yaml", "rule:fastapi", "package.json:scripts.build"


@dataclass
class DetectedParams:
    """100% 파일 파싱으로 결정 — LLM 없음."""
    project_root: str                                               # "" or "subdir/"
    framework: str | None                                           # "nextjs", "python-fastapi", ...
    runtime: Literal["interpreted", "compiled", "static"] | None
    package_manager: str                                            # "npm" | "pnpm" | "pip" | ...
    lockfile: str | None                                            # "pnpm-lock.yaml" | None
    standalone: bool                                                # next.config output:'standalone'
    req_file: str | None                                            # "requirements.txt" | "pyproject.toml"
    has_public_dir: bool
    has_go_sum: bool
    has_yarnrc: bool                                                # .yarnrc.yml 존재
    has_yarn_releases: bool                                         # .yarn/releases/ 존재
    entry_point: str | None                                         # "main.py" | "app.py" | None


@dataclass
class BuildParams:
    """Rule Engine 결과 — 각 필드에 confidence 부여."""
    detected: DetectedParams
    base_image:   RuleResult[str]
    runner_image: RuleResult[str]
    install_cmd:  RuleResult[str]
    build_cmd:    RuleResult[str | None]
    start_cmd:    RuleResult[list[str]]
    port:         RuleResult[int]
    build_output: RuleResult[str | None]
    env_vars:     dict[str, str] = field(default_factory=dict)

    def has_unknowns(self) -> bool:
        return any(
            getattr(self, f).confidence == "unknown"
            for f in ("install_cmd", "start_cmd", "port")
        )

    def unknown_fields(self) -> list[str]:
        return [
            f for f in ("install_cmd", "build_cmd", "start_cmd", "port")
            if getattr(self, f).confidence == "unknown"
        ]
