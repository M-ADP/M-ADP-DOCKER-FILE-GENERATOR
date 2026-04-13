from src.core.generators import BaseDockerfileGenerator
from src.infra.llm.dockerfile_generator import DockerfileGenerator


def get_dockerfile_generator() -> BaseDockerfileGenerator:
    return DockerfileGenerator()
