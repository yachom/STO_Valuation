"""graphflow 에이전트 베이스 — 자산 무관 BaseAgent/유틸(infra) + 부동산 도메인 프롬프트.

자산 무관 공통(normalize_text/short_hash/coerce_json/format_paths/BaseAgent)은
infra.agent.base에 있다. 여기엔 부동산 도메인 시스템 프롬프트(프레임 + 계층)만
두고, 공통은 그대로 재노출한다 — 기존 `from real_estate.graphflow.agents.base import
BaseAgent, ...` 호출부는 변경 없음.
"""

from __future__ import annotations

from infra.agent.base import (  # noqa: F401  (재노출 — 기존 호출부 호환)
    BaseAgent,
    coerce_json,
    format_paths,
    normalize_text,
    short_hash,
)
from real_estate.graphflow.prompts import get_prompt

# 재노출 + 도메인 심볼을 공개 API로 명시 (정적분석 '미사용 import' 억제 + 의도 문서화).
__all__ = [
    "BaseAgent", "normalize_text", "short_hash", "coerce_json", "format_paths",
    "DOMAIN_HIERARCHY", "korean_system_prompt",
]

# 프롬프트 문구는 prompts.py(레지스트리)가 단일 진실 — 코드에 박지 않는다.
# 외부 재노출(agents/__init__) 호환용 상수. 실제 조립은 korean_system_prompt가
# 매 호출 get_prompt로 하므로 A/B variant(active override)가 반영된다.
DOMAIN_HIERARCHY = get_prompt("base.domain_hierarchy")


def korean_system_prompt(role_description: str) -> str:
    """상가 부동산 사회적 가치 평가 공통 시스템 프롬프트 + 역할 설명."""
    return (get_prompt("base.frame", role=role_description)
            + get_prompt("base.domain_hierarchy"))
