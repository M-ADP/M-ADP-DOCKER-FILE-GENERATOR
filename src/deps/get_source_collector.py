from src.core.guards import CompositeSecurityGuard
from src.core.source.collector import SourceCollector


def get_source_collector() -> SourceCollector:
    guard = CompositeSecurityGuard()
    return SourceCollector(security_guard=guard)
