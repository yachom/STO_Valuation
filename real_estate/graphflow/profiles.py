"""축별 엣지 가중치 프로파일 — StaticPolicy의 통과 우선순위 표 (설계 §5.2).

가중치는 엣지 **종류**에 붙는 통과 우선순위다(개별 노드 아님). walk가 같은 1홉
이웃이라도 어떤 종류의 관계로 닿았느냐에 따라 줍는 순서를 정한다. 곱셈 구조라
깊이 페널티가 내장된다 — 0.7 엣지를 3홉 건너면 0.34로 자연 감쇠.

절대값보다 순서(tier)가 중요: 상(0.8~1.0)/중(0.4~0.7)/하(0.1~0.3).
운용: 골든 질문으로 walk 결과를 눈검사 → "엉뚱한 축 증거가 낀다" 패턴이 보이면
해당 엣지를 한 tier 조정. 표는 코드라 git으로 이력 관리.
"""

from __future__ import annotations

from real_estate.graphflow.graph.schema import EDGE_TYPES as E

# 프로파일에 없는 엣지 종류의 기본 가중치 — 하드 프루닝 대신 강한 페널티.
DEFAULT_WEIGHT = 0.05


PROFILES: dict[str, dict[str, float]] = {
    # ── 임차/임대 가치 ──
    "tenant": {
        E.ANCHORS: 1.0,
        E.HAS_TENANCY: 0.9,
        E.MENTIONS: 0.7,
        E.ABOUT: 0.5,
        E.EXTRACTED_FROM: 0.5,
        E.LOCATED_IN: 0.5,
        E.WITHIN_RADIUS: 0.4,
        E.SERVES_DISTRICT: 0.3,
        E.TOP_INDUSTRY: 0.3,
        E.CERTIFIED_AS: 0.1,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── ESG / 물리적 상태 ──
    "esg": {
        E.ANCHORS: 1.0,
        E.CERTIFIED_AS: 0.9,
        E.MENTIONS: 0.7,
        E.ABOUT: 0.5,
        E.EXTRACTED_FROM: 0.5,
        E.LOCATED_IN: 0.3,
        E.SERVES_DISTRICT: 0.2,
        E.TOP_INDUSTRY: 0.1,
        E.HAS_TENANCY: 0.1,
        E.WITHIN_RADIUS: 0.1,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── 입지 / 상권 ──
    "commercial": {
        E.ANCHORS: 1.0,
        E.SERVES_DISTRICT: 0.9,
        E.TOP_INDUSTRY: 0.8,
        E.MENTIONS: 0.7,
        E.ABOUT: 0.6,
        E.WITHIN_RADIUS: 0.6,
        E.EXTRACTED_FROM: 0.5,
        E.LOCATED_IN: 0.5,
        E.HAS_TENANCY: 0.2,
        E.CERTIFIED_AS: 0.1,
        E.SAME_AS_CANDIDATE: 0.1,
    },
    # ── QA 베이스라인 (LLMPolicy boost의 출발점, PR-E) ──
    "qa_base": {
        E.ANCHORS: 1.0,
        E.MENTIONS: 0.7,
        E.HAS_TENANCY: 0.6,
        E.SERVES_DISTRICT: 0.6,
        E.TOP_INDUSTRY: 0.6,
        E.CERTIFIED_AS: 0.6,
        E.ABOUT: 0.6,
        E.WITHIN_RADIUS: 0.5,
        E.EXTRACTED_FROM: 0.5,
        E.LOCATED_IN: 0.4,
        E.SAME_AS_CANDIDATE: 0.1,
    },
}

# 보고서 집필이 도는 3축 (Composer가 순회)
REPORT_AXES = ("tenant", "esg", "commercial")
