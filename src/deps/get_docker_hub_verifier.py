from src.infra.llm.docker_hub_verifier import DockerHubVerifier


def get_docker_hub_verifier() -> DockerHubVerifier:
    return DockerHubVerifier()
