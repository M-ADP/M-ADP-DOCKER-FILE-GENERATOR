from dataclasses import dataclass


@dataclass
class SourceFile:
    path: str
    content: str
    priority: int  # 낮을수록 높은 우선순위
