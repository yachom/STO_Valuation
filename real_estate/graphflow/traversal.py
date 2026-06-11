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
                   m.temporal_kind AS temporal_kind, m.confidence AS confidence
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
                       "temporal_kind": r["temporal_kind"], "confidence": r["confidence"]},
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


# ───────────────────────────────────────────────────────────
# LLMPolicy 적응 순회 — llm_walk (설계 §5.3, D4)
#
# 엔진 프리미티브(_expand/_load_nodes/EvidencePath)는 StaticPolicy walk와
# 공유하고, "이 노드를 더 갈까" 판단만 LLM이 홉당 1회 배치로 한다.
# 점수 산정은 qa_base StaticPolicy(랭킹 일관성) — LLM은 가지치기/확장만 결정.
# 비결정적이므로 보고서 집필에는 쓰지 않는다 (QA·확장 전용).
# ───────────────────────────────────────────────────────────
_JUDGE_SYSTEM = (
    "당신은 지식 그래프 탐색 판정자입니다. 목적(질문)에 비추어 각 후보 노드를 "
    "판정합니다:\n"
    "- keep: 이 노드 자체가 답의 근거가 된다\n"
    "- expand: 이 노드 너머에 근거가 있을 듯하다 — 이웃을 더 탐색\n"
    "- both: 근거이면서 더 탐색할 가치도 있다\n"
    "- drop: 목적과 무관\n"
    "탐색 예산이 제한적이니 무관한 노드는 과감히 drop하세요. "
    "목록에 없는 번호는 만들지 마세요."
)


def _get_walk_judge():
    """walk_judge 에이전트 (지연 생성 — traversal은 평소 LLM 무의존)."""
    global _walk_judge
    try:
        return _walk_judge
    except NameError:
        from real_estate.graphflow.agents.base import BaseAgent

        class _WalkJudge(BaseAgent):
            name = "walk_judge"

        _walk_judge = _WalkJudge()
        return _walk_judge


def llm_walk(
    driver: Driver,
    anchor_eids: list[str],
    purpose: str,
    *,
    max_hops: int = 3,
    k: int = 12,
    max_candidates_per_hop: int = 24,
) -> list[EvidencePath]:
    """적응적 깊이 순회 — LLM이 홉당 후보를 일괄 판정해 keep/expand 결정.

    frontier가 비면 MAX 홉 전에 자연 조기 종료. 판정 결과는 trace에 남는다.
    """
    from pydantic import BaseModel, Field, field_validator

    from real_estate.graphflow.agents.base import coerce_json
    from real_estate.graphflow.policies import StaticPolicy
    from real_estate.graphflow.tracing import emit

    class _Verdict(BaseModel):
        index: int
        verdict: str = Field(description="keep | expand | both | drop")

    class _JudgeOutput(BaseModel):
        verdicts: list[_Verdict]

        @field_validator("verdicts", mode="before")
        @classmethod
        def _coerce(cls, v):
            return coerce_json(v)

    static = StaticPolicy("qa_base")           # 점수 산정용 (판정과 분리)
    judge = _get_walk_judge()

    anchors = _load_nodes(driver, anchor_eids)
    best: dict[str, EvidencePath] = {}
    frontier: list[EvidencePath] = []
    for eid, nr in anchors.items():
        p = EvidencePath([nr], [], 1.0)
        best[eid] = p
        frontier.append(p)
    kept: dict[str, EvidencePath] = {}

    for hop in range(1, max_hops + 1):
        if not frontier:
            break
        neighbors = _expand(driver, [p.tail.eid for p in frontier])
        # 후보 구성 (노드별 최고 점수 경로, 정렬·상한)
        cands: dict[str, EvidencePath] = {}
        for p in frontier:
            path_eids = {n.eid for n in p.nodes}
            for nb in neighbors.get(p.tail.eid, []):
                if nb.noderef.eid in path_eids:
                    continue
                w = static.edge_weight(nb.rel, nb.direction)
                if w <= 0:
                    continue
                score = p.score * w * static.node_score(nb.noderef.label, nb.props)
                cur = best.get(nb.noderef.eid)
                if cur is not None and score <= cur.score + _EPS:
                    continue
                npath = EvidencePath(
                    p.nodes + [nb.noderef],
                    p.edges + [EdgeStep(nb.rel, nb.direction, w)], score)
                prev = cands.get(nb.noderef.eid)
                if prev is None or score > prev.score:
                    cands[nb.noderef.eid] = npath
        ranked = sorted(cands.values(), key=lambda p: (-p.score, p.sig))
        ranked = ranked[:max_candidates_per_hop]
        if not ranked:
            break

        lines = [f"[{i}] ({p.tail.label}) {p.tail.card_text[:200]}"
                 for i, p in enumerate(ranked)]
        try:
            out = judge.complete(
                system=_JUDGE_SYSTEM,
                user=f"[목적]\n{purpose}\n\n[후보 노드]\n" + "\n".join(lines),
                schema=_JudgeOutput)
            vmap = {v.index: v.verdict for v in out.verdicts
                    if 0 <= v.index < len(ranked)}
        except Exception as exc:
            # 판정 실패 → 결정적 폴백: 전부 keep + 점수 상위 3개만 확장
            emit("walk_judge_fallback", hop=hop, error=str(exc)[:200])
            vmap = {i: ("both" if i < 3 else "keep") for i in range(len(ranked))}

        next_frontier: list[EvidencePath] = []
        n_keep = n_expand = 0
        for i, p in enumerate(ranked):
            verdict = vmap.get(i, "drop")
            best[p.tail.eid] = p
            if verdict in ("keep", "both"):
                prev = kept.get(p.tail.eid)
                if prev is None or p.score > prev.score:
                    kept[p.tail.eid] = p
                n_keep += 1
            if verdict in ("expand", "both"):
                next_frontier.append(p)
                n_expand += 1
        emit("walk_judge", hop=hop, candidates=len(ranked),
             kept=n_keep, expanded=n_expand,
             dropped=len(ranked) - n_keep - n_expand)
        frontier = next_frontier

    results = sorted(kept.values(), key=lambda p: (-p.score, p.tail.label, p.tail.key))
    logger.info("llm_walk(%d anchors, hops≤%d) → %d kept", len(anchor_eids),
                max_hops, len(results))
    return results[:k]


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
