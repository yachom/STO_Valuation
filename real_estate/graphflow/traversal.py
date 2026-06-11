"""GraphWalk 엔진 — 앵커에서 출발해 가중 경로로 증거를 모으는 단일 프리미티브.

확장(어디를 더 팔까)·집필(이 섹션의 근거)·QA(이 질문의 근거)가 모두 같은 질문
("앵커에서 의미 있는 경로로 모은 증거는 무엇인가")이므로 엔진은 하나다 (설계 §5).
판단부(policy)만 교체한다 — StaticPolicy(결정적, 보고서)/LLMPolicy(적응, QA).

경로 점수 = ∏ 엣지가중치 × 노드 유효성·신뢰도 (§5.4). 가중치/배수가 모두 ≤1이라
경로를 늘릴수록 점수가 단조 감소 → 노드별 최고 점수 경로를 일찍 확정한다.
재현성(설계 PR-B 게이트): 쿼리에 ORDER BY, 동점은 (label,key)로 결정적 정렬.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from neo4j import Driver

from real_estate.graphflow.graph.schema import KEY_PROP
from real_estate.graphflow.policies import WalkPolicy

logger = logging.getLogger("graphflow.walk")

_EPS = 1e-12


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
def _load_nodes(driver: Driver, eids: list[str]) -> dict[str, NodeRef]:
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
            eids=eids, keymap=KEY_PROP,
        )
        return {
            r["eid"]: NodeRef(r["eid"], r["label"], str(r["nat_key"]), r["card"])
            for r in result
        }


def _expand(driver: Driver, eids: list[str]) -> dict[str, list[_Neighbor]]:
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
                   m.confidence AS confidence
            ORDER BY from_eid, to_eid, rel
            """,
            eids=eids, keymap=KEY_PROP,
        )
        for r in result:
            nb = _Neighbor(
                rel=r["rel"],
                direction="out" if r["outgoing"] else "in",
                noderef=NodeRef(r["to_eid"], r["to_label"], str(r["nat_key"]), r["card"]),
                props={"published_at": r["published_at"], "valid_until": r["valid_until"],
                       "confidence": r["confidence"]},
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
    max_hops: int = 3,
    k: int = 12,
) -> list[EvidencePath]:
    """앵커에서 max_hops 이내 가중 BFS로 증거 경로 top-k 반환.

    가중치·노드배수가 ≤1이라 경로 연장 시 점수 단조 감소 → 노드별 최고 점수
    경로를 relaxation으로 확정. 동점은 경로 sig로 결정적 선택(재현성).
    """
    anchors = _load_nodes(driver, anchor_eids)
    anchor_set = set(anchors)

    best: dict[str, EvidencePath] = {}
    frontier: list[EvidencePath] = []
    for eid, nr in anchors.items():
        p = EvidencePath([nr], [], 1.0)
        best[eid] = p
        frontier.append(p)

    for _ in range(max_hops):
        if not frontier:
            break
        neighbors = _expand(driver, [p.tail.eid for p in frontier])
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
                    best[nb.noderef.eid] = npath
                    prev = chosen.get(nb.noderef.eid)
                    if prev is None or score > prev.score + _EPS:
                        chosen[nb.noderef.eid] = npath
        frontier = list(chosen.values())

    results = [p for eid, p in best.items() if eid not in anchor_set]
    results.sort(key=lambda p: (-p.score, p.tail.label, p.tail.key))
    logger.info("walk(%s, hops=%d) → %d paths", policy.name, max_hops, len(results))
    return results[:k]


# ───────────────────────────────────────────────────────────
# 앵커 해소 (설계 §5.6)
# ───────────────────────────────────────────────────────────
def asset_anchor_eid(driver: Driver, asset_id: str) -> str | None:
    """보고서 집필용 고정 앵커 — Asset 노드 elementId."""
    with driver.session() as s:
        r = s.run(
            "MATCH (a:Asset {asset_id: $aid}) RETURN elementId(a) AS eid",
            aid=asset_id,
        ).single()
        return r["eid"] if r else None


def _scope_eids(driver: Driver, asset_id: str, hops: int) -> set[str]:
    """Asset에서 hops 이내 노드 eid 집합 (QA 스코프 한정, 설계 §D11)."""
    with driver.session() as s:
        r = s.run(
            f"""
            MATCH (a:Asset {{asset_id: $aid}})
            MATCH (a)-[*0..{int(hops)}]-(m)
            RETURN collect(DISTINCT elementId(m)) AS eids
            """,
            aid=asset_id,
        ).single()
        return set(r["eids"]) if r else set()


def vector_anchors(
    driver: Driver,
    query: str,
    *,
    k: int = 3,
    per_label_k: int = 5,
    scope_asset_id: str | None = None,
    scope_hops: int = 3,
) -> list[str]:
    """질문 임베딩 → card_text 벡터 인덱스 검색 → 앵커 노드 eid (§5.6).

    scope_asset_id가 주어지면 Asset에서 scope_hops 이내로 후보를 한정한다
    (초기 탐색은 자산 스코프가 전부, 설계 §D11). 임베딩 미적재 라벨/없는 인덱스는
    건너뛴다.
    """
    from real_estate.graphflow.graph.embedder import embed_one
    from real_estate.graphflow.graph.vector_index import INDEXED_LABELS, search_similar

    qv = embed_one(query)
    by_eid: dict[str, dict] = {}
    for label in INDEXED_LABELS:
        try:
            for h in search_similar(driver, label, qv, k=per_label_k):
                prev = by_eid.get(h["eid"])
                if prev is None or h["score"] > prev["score"]:
                    by_eid[h["eid"]] = h
        except Exception as e:                       # 인덱스 부재/빈 라벨
            logger.debug("search_similar skip %s: %s", label, e)

    ranked = sorted(by_eid.values(), key=lambda h: (-h["score"], h["eid"]))
    if scope_asset_id:
        in_scope = _scope_eids(driver, scope_asset_id, scope_hops)
        ranked = [h for h in ranked if h["eid"] in in_scope]
    return [h["eid"] for h in ranked[:k]]
