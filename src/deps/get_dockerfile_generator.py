from fastapi import Depends

from src.core.generators import BaseDockerfileGenerator
from src.core.stack.detector import StackDetector
from src.core.stack.root_detector import ProjectRootDetector
from src.deps.get_build_validator import get_build_validator
from src.deps.get_docker_hub_verifier import get_docker_hub_verifier
from src.deps.get_hadolint_validator import get_hadolint_validator
from src.deps.get_nova_llm import get_nova_llm
from src.deps.get_spec_generator import get_spec_generator
from src.infra.docker.build_validator import DockerBuildValidator
from src.infra.llm.docker_hub_verifier import DockerHubVerifier
from src.infra.llm.dockerfile_generator import DockerfileGenerator
from src.infra.llm.nova import NovaLLM
from src.infra.llm.spec.generator import SpecGenerator
from src.infra.linting.hadolint import HadolintValidator


def get_dockerfile_generator(
    llm: NovaLLM = Depends(get_nova_llm),
    spec_generator: SpecGenerator = Depends(get_spec_generator),
    hadolint: HadolintValidator = Depends(get_hadolint_validator),
    build_validator: DockerBuildValidator = Depends(get_build_validator),
    verifier: DockerHubVerifier = Depends(get_docker_hub_verifier),
) -> BaseDockerfileGenerator:
    return DockerfileGenerator(
        llm=llm,
        spec_generator=spec_generator,
        hadolint=hadolint,
        build_validator=build_validator,
        verifier=verifier,
        stack_detector=StackDetector(),
        root_detector=ProjectRootDetector(),
    )
