"""WalkPolicy (음악저작권) — walk 엔진의 "이 노드를 더 확장할까/이 엣지를 얼마로
칠까" 판단부. real_estate/graphflow/policies.py를 미러링한다.

엔진은 하나(infra.graph.walk.walk), 판단부만 교체한다 (설계 §5.2/§5.3):
    - StaticPolicy : 엣지 종류별 가중치 표. 결정적 → 보고서 집필(재현성)
    - (LLMPolicy/SemanticPolicy는 도메인 확장 시 추가 — 여기선 StaticPolicy만)

`WalkPolicy`(프로토콜)는 infra가 소유하고, 구체 정책은 자산이 소유한다 — 여기서
StaticPolicy가 그 프로토콜을 만족(isinstance 검증으로 합성 증명)한다.

⚠️ TODO(domain): node_score는 지금 confidence 배수만 본다. real_estate는 도메인
leaf인 validity(시점 감쇠 등)를 곱하지만, 그건 자산별 규칙이라 음악 validity가
정해지면 여기에 같은 형태로 붙인다(infra는 무관 — 합성에 영향 없음).
"""

from __future__ import annotations

from infra.graph.walk import WalkPolicy  # noqa: F401  (구체 정책의 프로토콜 — 재노출)
from music_copyright.graphflow.graph.schema import NODE_LABELS as _L
from music_copyright.graphflow.profiles import DEFAULT_WEIGHT, PROFILES

# 자산이 보유한 축별 가중치 표 별칭 (real_estate의 PROFILES와 동형 — 외부 노출용)
MUSIC_PROFILES = PROFILES

# 공유 참조/허브 라벨 — walk가 evidence로 도달은 하되 *통과(재확장)*하지 않는다
# (absorbing leaf). 아티스트·레이블·플랫폼·수상은 여러 곡이 공유하므로, 이를 통과하면
# 무관한 다른 곡으로 샌다(교차오염). 카드는 evidence로 쓰이되 그 너머는 안 판다.
# 확장 대상 = 곡 고유 수집물(Query/WebDoc/Fact/Entity) + 앵커 Work(초기 프론티어라 무관).
_HUB_LABELS = frozenset({
    _L.WORK, _L.ARTIST, _L.RIGHTSHOLDER, _L.PLATFORM,
    _L.AWARD, _L.CHART, _L.CERTIFICATION, _L.SOCIAL_METRIC,
})


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class StaticPolicy:
    """엣지 종류별 고정 가중치 표 기반 정책. 결정적 — 보고서 집필용.

    boost: 엣지 종류별 가감(QA에서 LLM이 제안하는 미세 조정). ±BOOST_CLAMP로
    제한해 LLM이 표를 과격하게 뒤집지 못하게 한다.
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
            # 미등록 축(동적)은 균형 prior(qa_base)로 폴백 — 수동 가중치 표가 없어도
            # SemanticPolicy의 의도 코사인이 관련 노드를 부상시킨다.
            self._weights = dict(PROFILES.get(profile, PROFILES["qa_base"]))
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
        """신뢰도(confidence) 배수 (0~1).

        TODO(domain): real_estate는 여기에 validity(유효성·시점 감쇠) 항을 곱한다.
        음악 도메인 validity가 정해지면 동일 형태로 추가한다. 지금은 confidence만.
        """
        score = 1.0
        conf = props.get("confidence")
        if isinstance(conf, (int, float)):
            score *= max(0.0, min(1.0, float(conf)))
        return score

    def expandable(self, label: str, props: dict) -> bool:
        """이 노드 *너머로* walk를 이어갈지 — 공유 허브는 leaf로 막아 교차오염 차단."""
        return label not in _HUB_LABELS


class SemanticPolicy:
    """엣지 prior(StaticPolicy) × 노드-축의도 의미유사도 정책 (real_estate 미러).

    엣지 종류 상수만으로는 '이 노드가 축 관심사와 의미적으로 가까운가'를 못 본다
    (같은 COVERED_BY면 무관한 커버도 관련 커버도 동급). node_score에 코사인 항을 곱해
    그 공백을 데이터로 메운다 — 가중치 표는 약한 구조 prior로 강등되고, 변별은
    의미유사도가 한다('relation-free + semantic bridging'). 결정적(임베딩·스코프 고정).

    sim_by_eid: traversal.semantic_sims (eid → [0,1] 정규화 유사도).
    alpha: 의미 비중. score *= (alpha*sim + (1-alpha)) — 1-alpha는 구조적으로 유효한
           노드가 의미 0이어도 죽지 않게 하는 바닥값.
    corrob_by_eid: traversal.corroboration_scores (eid → 독립 출처 수). × (1+beta·log(1+n)·sim)
           — sim으로 게이트해 '강하지만 무관한' 노드(타 작품 누출)는 부스트가 안 먹는다.
    """

    def __init__(
        self,
        profile: str | dict[str, float],
        sim_by_eid: dict[str, float],
        *,
        alpha: float = 0.6,
        corrob_by_eid: dict[str, float] | None = None,
        beta: float = 0.35,
        boost: dict[str, float] | None = None,
    ):
        self._static = StaticPolicy(profile, boost=boost)
        self._sim = sim_by_eid
        self._alpha = _clamp(alpha, 0.0, 1.0)
        self._corrob = corrob_by_eid or {}
        self._beta = max(0.0, beta)
        self.name = f"semantic:{self._static.name}"

    def edge_weight(self, rel: str, direction: str) -> float:
        return self._static.edge_weight(rel, direction)

    def expandable(self, label: str, props: dict) -> bool:
        return self._static.expandable(label, props)

    def node_score(self, label: str, props: dict) -> float:
        import math
        base = self._static.node_score(label, props)
        eid = props.get("_eid")
        sim = self._sim.get(eid, 0.0) if eid else 0.0
        score = base * (self._alpha * sim + (1.0 - self._alpha))
        if eid and self._corrob:
            srcs = self._corrob.get(eid, 0.0)
            score *= 1.0 + self._beta * math.log1p(srcs) * sim
        return score
