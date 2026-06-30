"""GraphWalk 엔진 — 앵커에서 출발해 가중 경로로 증거를 모으는 단일 프리미티브.

확장(어디를 더 팔까)·집필(이 섹션의 근거)·QA(이 질문의 근거)가 모두 같은 질문
("앵커에서 의미 있는 경로로 모은 증거는 무엇인가")이므로 엔진은 하나다 (설계 §5).
판단부(policy)만 교체한다 — StaticPolicy(결정적, 보고서)/LLMPolicy(적응, QA).

경로 점수 = ∏ 엣지가중치 × 노드 유효성·신뢰도 (§5.4). 가중치/배수가 모두 ≤1이라
경로를 늘릴수록 점수가 단조 감소 → 노드별 최고 점수 경로를 일찍 확정한다.
재현성(설계 PR-B 게이트): 쿼리에 ORDER BY, 동점은 (label,key)로 결정적 정렬.

자산 무관 엔진이다 — 라벨→자연키 매핑(key_props)을 인자로 받는다. 자산 패키지
(<asset>/graphflow/traversal.py)가 자신의 KEY_PROP를 주입하는 래퍼로 호출한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from neo4j import Driver

logger = logging.getLogger("infra.graph.walk")

_EPS = 1e-12


# ───────────────────────────────────────────────────────────
# WalkPolicy — walk 엔진의 "이 노드를 더 확장할까/이 엣지를 얼마로 칠까" 판단부.
#
# 엔진은 하나(walk), 판단부만 교체한다 (설계 §5.2/§5.3):
#     - StaticPolicy : 엣지 종류별 가중치 표. 결정적 → 보고서 집필(재현성 필요)
#     - LLMPolicy    : 노드 내용 vs 목적 관계를 LLM이 판정. 적응적 → QA/EXPAND
# 가중치는 방향 무관하게 엣지 종류로만 매긴다 (PR-B).
# (구체 정책은 자산 패키지에 둔다 — 여기선 프로토콜만.)
# ───────────────────────────────────────────────────────────
@runtime_checkable
class WalkPolicy(Protocol):
    name: str

    def edge_weight(self, rel: str, direction: str) -> float:
        """엣지 종류(+방향)의 통과 가중치. 0 이하면 walk가 가지를 친다."""
        ...

    def node_score(self, label: str, props: dict) -> float:
        """도착 노드의 신뢰/유효성 배수 (0~1). 경로 점수에 곱해진다."""
        ...


# ───────────────────────────────────────────────────────────
# 자료구조
# ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class NodeRef:
    eid: str
    label: str
    key: str
    card_text: str


@dataclass(frozen=True)
class EdgeStep:
    rel: str
    direction: str   # 'out' | 'in' (순회 방향 기준)
    weight: float


@dataclass
class EvidencePath:
    nodes: list[NodeRef]              # [anchor, ..., tail]
    edges: list[EdgeStep]            # len == len(nodes) - 1
    score: float

    @property
    def tail(self) -> NodeRef:
        return self.nodes[-1]

    @property
    def sig(self) -> tuple:
        """경로 식별자 (동점 tie-break / 재현성용)."""
        return tuple(n.key for n in self.nodes)

    @property
    def hops(self) -> int:
        return len(self.edges)

    def text(self) -> str:
        """LLM 컨텍스트용 직렬화 — 카드 체인 + 관계/방향 (§5.5)."""
        lines = [self.nodes[0].card_text]
        for i, e in enumerate(self.edges):
            nxt = self.nodes[i + 1]
            arrow = f"—[{e.rel}]→" if e.direction == "out" else f"←[{e.rel}]—"
            lines.append(f"   {arrow} {nxt.card_text}")
        return "\n".join(lines)


@dataclass
class _Neighbor:
    rel: str
    direction: str
    noderef: NodeRef
    props: dict = field(default_factory=dict)


# ───────────────────────────────────────────────────────────
# Neo4j 접근
# ───────────────────────────────────────────────────────────
def load_nodes(driver: Driver, eids: list[str], key_props: dict[str, str]) -> dict[str, NodeRef]:
    if not eids:
        return {}
    with driver.session() as s:
        result = s.run(
            """
            UNWIND $eids AS eid
            MATCH (n) WHERE elementId(n) = eid
            RETURN eid AS eid, labels(n)[0] AS label,
                   coalesce(n.card_text, '') AS card,
                   n[$keymap[labels(n)[0]]] AS nat_key
            ORDER BY eid
            """,
            eids=eids, keymap=key_props,
        )
        return {
            r["eid"]: NodeRef(r["eid"], r["label"], str(r["nat_key"]), r["card"])
            for r in result
        }


def _expand(driver: Driver, eids: list[str], key_props: dict[str, str]) -> dict[str, list[_Neighbor]]:
    """frontier eid들의 1홉 이웃(양방향). card_embedding은 전송하지 않는다."""
    if not eids:
        return {}
    out: dict[str, list[_Neighbor]] = {e: [] for e in eids}
    with driver.session() as s:
        result = s.run(
            """
            UNWIND $eids AS eid
            MATCH (n) WHERE elementId(n) = eid
            MATCH (n)-[r]-(m)
            WITH eid, r, n, m, labels(m)[0] AS lbl
            RETURN eid AS from_eid, type(r) AS rel, startNode(r) = n AS outgoing,
                   elementId(m) AS to_eid, lbl AS to_label,
                   coalesce(m.card_text, '') AS card,
                   m[$keymap[lbl]] AS nat_key,
                   m.published_at AS published_at, m.valid_until AS valid_until,
                   m.temporal_kind AS temporal_kind, m.confidence AS confidence,
                   m.source_tier AS source_tier, m.`앵커점수` AS anchor_score
            ORDER BY from_eid, to_eid, rel
            """,
            eids=eids, keymap=key_props,
        )
        for r in result:
            nb = _Neighbor(
                rel=r["rel"],
                direction="out" if r["outgoing"] else "in",
                noderef=NodeRef(r["to_eid"], r["to_label"], str(r["nat_key"]), r["card"]),
                props={"published_at": r["published_at"], "valid_until": r["valid_until"],
                       "temporal_kind": r["temporal_kind"], "confidence": r["confidence"],
                       "source_tier": r["source_tier"],
                       "앵커점수": r["anchor_score"], "_eid": r["to_eid"]},
            )
            out[r["from_eid"]].append(nb)
    return out


# ───────────────────────────────────────────────────────────
# walk 엔진
# ───────────────────────────────────────────────────────────
def walk(
    driver: Driver,
    anchor_eids: list[str],
    policy: WalkPolicy,
    *,
    key_props: dict[str, str],
    max_hops: int = 3,
    k: int = 12,
) -> list[EvidencePath]:
    """앵커에서 max_hops 이내 가중 BFS로 증거 경로 top-k 반환.

    가중치·노드배수가 ≤1이라 경로 연장 시 점수 단조 감소 → 노드별 최고 점수
    경로를 relaxation으로 확정. 동점은 경로 sig로 결정적 선택(재현성).
    """
    anchors = load_nodes(driver, anchor_eids, key_props)
    anchor_set = set(anchors)

    # 선택적 확장 게이트(policy.expandable) — 어떤 노드는 evidence로 도달은 되되 그
    # 너머로 *통과(재확장)*하지 않게 한다(absorbing leaf). 공유 허브(아티스트·레이블·
    # 플랫폼 등)를 통해 무관한 다른 앵커의 서브그래프로 새는 걸 막는 용도. 미정의
    # policy는 전부 확장(하위호환 — 기존 StaticPolicy/SemanticPolicy 무영향).
    expandable = getattr(policy, "expandable", None)

    best: dict[str, EvidencePath] = {}
    frontier: list[EvidencePath] = []
    for eid, nr in anchors.items():
        p = EvidencePath([nr], [], 1.0)
        best[eid] = p
        frontier.append(p)

    for _ in range(max_hops):
        if not frontier:
            break
        neighbors = _expand(driver, [p.tail.eid for p in frontier], key_props)
        chosen: dict[str, EvidencePath] = {}   # 이번 홉에 best가 갱신된 노드 → 재확장 대상
        for p in frontier:
            path_eids = {n.eid for n in p.nodes}
            for nb in neighbors.get(p.tail.eid, []):
                if nb.noderef.eid in path_eids:        # 같은 경로 내 재방문 금지
                    continue
                w = policy.edge_weight(nb.rel, nb.direction)
                if w <= 0:
                    continue
                score = p.score * w * policy.node_score(nb.noderef.label, nb.props)
                npath = EvidencePath(
                    p.nodes + [nb.noderef],
                    p.edges + [EdgeStep(nb.rel, nb.direction, w)],
                    score,
                )
                cur = best.get(nb.noderef.eid)
                better = (
                    cur is None
                    or score > cur.score + _EPS
                    or (abs(score - cur.score) <= _EPS and npath.sig < cur.sig)
                )
                if better:
                    best[nb.noderef.eid] = npath   # evidence 후보로는 항상 보존
                    # 확장 게이트 — 비확장 노드는 다음 프론티어에 안 넣어 통과 차단.
                    if expandable is None or expandable(nb.noderef.label, nb.props):
                        prev = chosen.get(nb.noderef.eid)
                        if prev is None or score > prev.score + _EPS:
                            chosen[nb.noderef.eid] = npath
        frontier = list(chosen.values())

    results = [p for eid, p in best.items() if eid not in anchor_set]
    results.sort(key=lambda p: (-p.score, p.tail.label, p.tail.key))
    logger.info("walk(%s, hops=%d) → %d paths", policy.name, max_hops, len(results))
    return results[:k]
