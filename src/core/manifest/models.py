from dataclasses import dataclass, field


@dataclass
class ManifestInfo:
    language: str
    runtime_version: str | None
    dependencies: dict[str, str]
    dev_dependencies: dict[str, str]
    scripts: dict[str, str]
    pkg_manager: str
    pkg_manager_version: str | None
    entry_point: str | None
    detected_port: int | None
    raw_deps: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
