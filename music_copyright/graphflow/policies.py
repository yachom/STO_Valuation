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
from music_copyright.graphflow.profiles import DEFAULT_WEIGHT, PROFILES

# 자산이 보유한 축별 가중치 표 별칭 (real_estate의 PROFILES와 동형 — 외부 노출용)
MUSIC_PROFILES = PROFILES


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
        """신뢰도(confidence) 배수 (0~1).

        TODO(domain): real_estate는 여기에 validity(유효성·시점 감쇠) 항을 곱한다.
        음악 도메인 validity가 정해지면 동일 형태로 추가한다. 지금은 confidence만.
        """
        score = 1.0
        conf = props.get("confidence")
        if isinstance(conf, (int, float)):
            score *= max(0.0, min(1.0, float(conf)))
        return score
