from src.infra.linting.hadolint import HadolintValidator


def get_hadolint_validator() -> HadolintValidator:
    return HadolintValidator()
