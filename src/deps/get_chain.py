from langchain_aws import ChatBedrockConverse
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from src.common.config.nova import NovaSettings

SYSTEM_PROMPT = """당신은 Dockerfile 전문가입니다.
주어진 소스코드를 분석하여 production-ready Dockerfile을 생성하세요.

규칙:
- 멀티스테이지 빌드 사용 (빌드 환경 ≠ 런타임 환경)
- Alpine/Slim 경량 이미지 사용
- 레이어 캐시 최적화 (의존성 설치 → 소스 복사 순서)
- WORKDIR /app 고정
- 비루트 사용자 설정
- EXPOSE 포트 명시
- Dockerfile 텍스트만 반환 (설명, 마크다운 코드블록 없이)"""

HUMAN_PROMPT = """[소스코드]
{source_code}

위 소스코드를 기반으로 Dockerfile을 생성해주세요."""


def get_chain() -> Runnable:
    settings = NovaSettings()
    llm = ChatBedrockConverse(
        model=settings.bedrock_model_id,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        region_name=settings.bedrock_region,
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", HUMAN_PROMPT),
    ])
    return prompt | llm | StrOutputParser()
