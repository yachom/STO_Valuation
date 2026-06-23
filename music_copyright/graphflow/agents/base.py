"""music_copyright 에이전트 베이스 — 자산 무관 BaseAgent/유틸(infra) + 음악 도메인 프롬프트.

real_estate/graphflow/agents/base.py와 동일 패턴. 자산 무관 공통
(normalize_text/short_hash/coerce_json/format_paths/BaseAgent)은 infra.agent.base에
있다 — 그대로 재노출한다(복제 없음). 여기엔 음악 도메인 시스템 프롬프트 조립만 둔다.

에이전트는 클래스변수 name만 지정하면 된다:
    class MusicWriter(BaseAgent):
        name = "music_writer"
"""

from __future__ import annotations

from infra.agent.base import (  # noqa: F401  (재노출 — 호출부 호환)
    BaseAgent,
    coerce_json,
    format_paths,
    normalize_text,
    short_hash,
)
from music_copyright.graphflow.prompts import get_prompt

__all__ = [
    "BaseAgent", "normalize_text", "short_hash", "coerce_json", "format_paths",
    "music_system_prompt",
]


def music_system_prompt(role_description: str) -> str:
    """음악저작물 사회·문화 가치 평가 공통 시스템 프롬프트 + 역할 설명.

    매 호출 get_prompt로 조립하므로 A/B variant(active override)가 반영된다.
    real_estate는 여기에 도메인 위계(등급 순서)를 덧붙이지만, 음악 위계는 도메인
    확정 후 추가한다(# TODO(domain)).
    """
    return get_prompt("base.frame", role=role_description)
