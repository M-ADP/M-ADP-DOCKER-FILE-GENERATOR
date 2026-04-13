from src.core.guards import CompositeSecurityGuard


def get_composite_guard() -> CompositeSecurityGuard:
    return CompositeSecurityGuard()
