from pydantic import BaseModel, Field


class CopyInstruction(BaseModel):
    sources: list[str] = Field(description="복사할 소스 경로 목록")
    destination: str = Field(description="컨테이너 내 목적지 경로")
    from_stage: str | None = Field(default=None, description="--from=<stage> 대상 스테이지")


class Stage(BaseModel):
    name: str = Field(description="스테이지 이름 (builder, runner 등)")
    base_image: str = Field(description="FROM 이미지 태그")
    workdir: str = Field(default="/app", description="WORKDIR 경로")
    copy_instructions: list[CopyInstruction] = Field(default_factory=list)
    run_commands: list[str] = Field(
        default_factory=list,
        description="개별 명령어 목록 (&&는 생성 단계에서 처리)",
    )
    env_vars: dict[str, str] = Field(default_factory=dict)
    user: str | None = Field(default=None)
    expose_port: int | None = Field(default=None)
    cmd: list[str] | None = Field(default=None)


class BuildSpec(BaseModel):
    detected_stack: str = Field(description="감지된 스택 이름")
    stages: list[Stage] = Field(description="멀티스테이지 빌드 스테이지 목록")
    pkg_manager: str = Field(description="패키지 매니저")
    pkg_manager_install_cmd: str = Field(description="의존성 설치 명령어")
    pkg_manager_build_cmd: str | None = Field(default=None, description="빌드 명령어")
    project_root: str = Field(default="", description="프로젝트 루트 상대 경로")
    reasoning: str = Field(default="", description="스펙 결정 근거")
