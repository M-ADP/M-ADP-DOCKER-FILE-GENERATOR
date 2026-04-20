from fastapi import Depends

from src.core.generators import BaseDockerfileGenerator
from src.deps.get_build_validator import get_build_validator
from src.deps.get_docker_hub_verifier import get_docker_hub_verifier
from src.deps.get_hadolint_validator import get_hadolint_validator
from src.deps.get_nova_llm import get_nova_llm
from src.infra.docker.build_validator import DockerBuildValidator
from src.infra.llm.docker_hub_verifier import DockerHubVerifier
from src.infra.llm.dockerfile_generator import DockerfileGenerator
from src.infra.llm.nova import NovaLLM
from src.infra.linting.hadolint import HadolintValidator


def get_dockerfile_generator(
    llm: NovaLLM = Depends(get_nova_llm),
    hadolint: HadolintValidator = Depends(get_hadolint_validator),
    build_validator: DockerBuildValidator = Depends(get_build_validator),
    verifier: DockerHubVerifier = Depends(get_docker_hub_verifier),
) -> BaseDockerfileGenerator:
    return DockerfileGenerator(
        llm=llm,
        hadolint=hadolint,
        build_validator=build_validator,
        verifier=verifier,
    )
