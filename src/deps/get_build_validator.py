from src.infra.docker.build_validator import DockerBuildValidator


def get_build_validator() -> DockerBuildValidator:
    return DockerBuildValidator()
