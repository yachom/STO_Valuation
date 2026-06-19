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
        """유효성(§12 ValidityCheck) × 신뢰도(§9.3 confidence) 배수."""
        from real_estate.graphflow.validity import validity
        score = validity(props, label=label).score
        conf = props.get("confidence")
        if isinstance(conf, (int, float)):
            score *= max(0.0, min(1.0, float(conf)))
        # 앵커점수가 있는 노드(Tenant)는 그 점수를 신뢰 배수로 반영 →
        # walk가 같은 HAS_TENANT 엣지여도 앵커 테넌트를 우선 표면화(주차장·일반 후순위).
        anchor = props.get("앵커점수")
        if isinstance(anchor, (int, float)):
            score *= 0.4 + 0.6 * max(0.0, min(1.0, float(anchor)))
        return score


class SemanticPolicy:
    """엣지 prior(StaticPolicy) × 노드-목적 의미유사도 정책 (방법1, GraphRAG 1단계).

    엣지 종류 상수만으로는 '이 노드가 목적과 의미적으로 가까운가'를 못 본다(발산
    상권이든 강남 상권이든 같은 SERVES_DISTRICT면 동급). node_score에 코사인 항을
    곱해 그 공백을 데이터로 메운다. 결정적(임베딩·스코프 고정) → 보고서 집필에도
    안전. 엣지 가중은 기존 프로파일을 거친 구조 prior로 재사용하고, 미세조정은
    의미유사도가 한다 — LinearRAG의 'relation-free + semantic bridging' 사상의
    경량 1단계 버전(PPR은 2단계, traversal 교체 시).

    sim_by_eid: traversal.semantic_sims 결과 (eid → [0,1] 정규화 유사도).
    alpha: 의미 비중. score *= (alpha*sim + (1-alpha)) — 1-alpha는 구조적으로
           유효한 노드가 의미 0이어도 완전히 죽지 않게 하는 바닥값.
    """

    def __init__(
        self,
        profile: str | dict[str, float],
        sim_by_eid: dict[str, float],
        *,
        alpha: float = 0.6,
        boost: dict[str, float] | None = None,
    ):
        self._static = StaticPolicy(profile, boost=boost)
        self._sim = sim_by_eid
        self._alpha = _clamp(alpha, 0.0, 1.0)
        self.name = f"semantic:{self._static.name}"

    def edge_weight(self, rel: str, direction: str) -> float:
        return self._static.edge_weight(rel, direction)

    def node_score(self, label: str, props: dict) -> float:
        base = self._static.node_score(label, props)
        eid = props.get("_eid")
        sim = self._sim.get(eid, 0.0) if eid else 0.0
        return base * (self._alpha * sim + (1.0 - self._alpha))
