"""노드 텍스트 카드 — 라벨→빌더 레지스트리 메커니즘 (자산 무관).

카드 *내용*(라벨별 한국어 요약 템플릿)은 도메인이다. 여기엔 라벨→빌더 dict로
디스패치하는 작은 메커니즘만 둔다. 각 자산은 자신의 card_for_* 빌더 MAP을 만들어
`build_card(label, props, registry)`로 호출한다.

카드는 결정적 템플릿이라 LLM 호출 없이 재현 가능하다. 카드 품질이 곧 QA 앵커 매칭
품질이므로 (설계 §5.6) 자산 측 빌더가 성의 있게 채운다.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger("infra.graph.cards")

CardBuilder = Callable[[dict], str]


def build_card(label: str, props: dict, registry: dict[str, CardBuilder]) -> str:
    """라벨+프로퍼티로 카드 텍스트 생성. 레지스트리에 빌더 미정의 라벨은 빈 문자열."""
    builder = registry.get(label)
    if builder is None:
        logger.warning("카드 빌더 미정의: %s", label)
        return ""
    return builder(props)
