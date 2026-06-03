"""LLM 어댑터 — Anthropic Claude 단일 프로바이더, 등급별 모델 매핑.

에이전트와 retriever는 이 모듈의 build_llm(tier)만을 사용해 LLM 인스턴스를 받는다.
구체 모델 ID는 한 곳에서만 관리.

등급:
    - OPUS: planner / synthesizer 등 핵심 판단
    - SONNET: 축별 analyst / validator / reviser
    - HAIKU: 노드 카드/요약 polish, 단순 추출

레퍼런스: [[tech_stack]]
"""

from __future__ import annotations

from enum import Enum

from langchain_anthropic import ChatAnthropic


class ModelTier(str, Enum):
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


# 단일 source of truth for model IDs.
# Sonnet 4.6 / Opus 4.7 / Haiku 4.5 — Claude 4.X 계열.
_TIER_MODEL_ID = {
    ModelTier.OPUS: "claude-opus-4-7",
    ModelTier.SONNET: "claude-sonnet-4-6",
    ModelTier.HAIKU: "claude-haiku-4-5-20251001",
}

# 기본 budget. 호출자가 override 가능.
# 주의: Claude 4.7 (Opus)에선 temperature 파라미터가 deprecated이라
# 기본값에서 temperature 제거. 필요 시 호출자가 override.
# structured output(JSON)에 토큰을 너무 적게 주면 잘려서 ValidationError가 나므로
# Sonnet/Opus 모두 8000으로 충분히 잡는다.
_TIER_DEFAULTS = {
    ModelTier.OPUS: {"max_tokens": 8000},
    ModelTier.SONNET: {"max_tokens": 8000},
    ModelTier.HAIKU: {"max_tokens": 2048},
}


def build_llm(tier: ModelTier, **overrides) -> ChatAnthropic:
    """등급별 ChatAnthropic 인스턴스. ANTHROPIC_API_KEY 환경변수 필수."""
    params = {**_TIER_DEFAULTS[tier], **overrides}
    return ChatAnthropic(model=_TIER_MODEL_ID[tier], **params)


def model_id_for(tier: ModelTier) -> str:
    return _TIER_MODEL_ID[tier]
