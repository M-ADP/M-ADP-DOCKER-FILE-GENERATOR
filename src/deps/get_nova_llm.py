from src.infra.llm.gemini import GeminiLLM


def get_nova_llm() -> GeminiLLM:
    return GeminiLLM()
