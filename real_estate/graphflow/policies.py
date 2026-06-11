"""WalkPolicy — walk 엔진의 "이 노드를 더 확장할까/이 엣지를 얼마로 칠까" 판단부.

엔진은 하나(traversal.walk), 판단부만 교체한다 (설계 §5.2/§5.3):
    - StaticPolicy : 엣지 종류별 가중치 표. 결정적 → 보고서 집필(재현성 필요)
    - LLMPolicy    : 노드 내용 vs 목적 관계를 LLM이 판정. 적응적 → QA/EXPAND
                     (PR-E에서 구현. 여기선 프로토콜만 정의)

가중치는 방향 무관하게 엣지 종류로만 매긴다 (PR-B). 자산 스코프 그래프는
대부분 Asset에서 바깥으로 뻗는 구조라 outward 순회가 자연스럽다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from real_estate.graphflow.profiles import DEFAULT_WEIGHT, PROFILES


@runtime_checkable
class WalkPolicy(Protocol):
    name: str

    def edge_weight(self, rel: str, direction: str) -> float:
        """엣지 종류(+방향)의 통과 가중치. 0 이하면 walk가 가지를 친다."""
        ...

    def node_score(self, label: str, props: dict) -> float:
        """도착 노드의 신뢰/유효성 배수 (0~1). 경로 점수에 곱해진다."""
        ...


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class StaticPolicy:
    """엣지 종류별 고정 가중치 표 기반 정책. 결정적 — 보고서 집필용.

    boost: 엣지 종류별 가감 (QA에서 LLM이 제안하는 미세 조정). ±BOOST_CLAMP로
    제한해 LLM이 표를 과격하게 뒤집지 못하게 한다 (설계 §5.7).
    """

    BOOST_CLAMP = 0.3

    def __init__(
        self,
        profile: str | dict[str, float],
        *,
        default_weight: float = DEFAULT_WEIGHT,
        boost: dict[str, float] | None = None,
    ):
        if isinstance(profile, str):
            self.name = profile
            self._weights = dict(PROFILES[profile])
        else:
            self.name = "custom"
            self._weights = dict(profile)
        self._default = default_weight
        self._boost = boost or {}

    def edge_weight(self, rel: str, direction: str) -> float:
        base = self._weights.get(rel, self._default)
        if rel in self._boost:
            base += _clamp(self._boost[rel], -self.BOOST_CLAMP, self.BOOST_CLAMP)
        return _clamp(base, 0.0, 1.0)

    def node_score(self, label: str, props: dict) -> float:
        # PR-B: 구조 노드만 존재 → 유효성/신뢰도 배수 1.0.
        # 확장/유효성 단계(§9.3 confidence, §12 ValidityCheck)에서 이 지점에
        # validity(node) × confidence(node)를 곱해 넣는다.
        return 1.0
