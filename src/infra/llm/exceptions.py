class LLMInfraException(Exception):
    """LLM 인프라 통신 오류. UseCase 레이어에서 AppException으로 변환된다."""
