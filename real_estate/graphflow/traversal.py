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

from neo4j import Driver

from infra.graph.embedder import embed_one
from infra.graph.walk import EdgeStep, EvidencePath, NodeRef  # noqa: F401  (호환 재노출)
from infra.graph.walk import _expand as _infra_expand
from infra.graph.walk import load_nodes as _infra_load_nodes
from infra.graph.walk import walk as _infra_walk
from real_estate.graphflow.graph.schema import KEY_PROP
from real_estate.graphflow.policies import WalkPolicy  # noqa: F401  (호환 재노출)

logger = logging.getLogger("graphflow.walk")

_EPS = 1e-12


# ───────────────────────────────────────────────────────────
# 엔진 래퍼 — infra.graph.walk 엔진에 도메인 KEY_PROP를 주입한다.
# 기존 호출부(`walk(driver, anchors, policy)` / `load_nodes(driver, eids)`)는 변경 없음.
# NodeRef/EdgeStep/EvidencePath/WalkPolicy는 위에서 재노출(import) — 기존
# `from real_estate.graphflow.traversal import EvidencePath, NodeRef` 호출부 호환.
# ───────────────────────────────────────────────────────────
def load_nodes(driver: Driver, eids: list[str]) -> dict[str, NodeRef]:
    return _infra_load_nodes(driver, eids, KEY_PROP)


def _expand(driver: Driver, eids: list[str]) -> dict:
    """frontier eid들의 1홉 이웃(양방향). KEY_PROP 주입 후 infra 엔진에 위임."""
    return _infra_expand(driver, eids, KEY_PROP)


def walk(
    driver: Driver,
    anchor_eids: list[str],
    policy: WalkPolicy,
    *,
    max_hops: int = 3,
    k: int = 12,
) -> list[EvidencePath]:
    """앵커에서 max_hops 이내 가중 BFS로 증거 경로 top-k 반환 (KEY_PROP 주입)."""
    return _infra_walk(driver, anchor_eids, policy, key_props=KEY_PROP,
                       max_hops=max_hops, k=k)


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


def scope_eids(driver: Driver, asset_id: str, hops: int) -> set[str]:
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
# 엔진 프리미티브(_expand/load_nodes/EvidencePath)는 StaticPolicy walk와
# 공유하고, "이 노드를 더 갈까" 판단만 LLM이 홉당 1회 배치로 한다.
# 점수 산정은 qa_base StaticPolicy(랭킹 일관성) — LLM은 가지치기/확장만 결정.
# 비결정적이므로 보고서 집필에는 쓰지 않는다 (QA·확장 전용).
# ───────────────────────────────────────────────────────────
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
    from infra.agent.tracing import emit

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

    anchors = load_nodes(driver, anchor_eids)
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
            from real_estate.graphflow.prompts import get_prompt
            out = judge.complete(
                system=get_prompt("walk_judge.system"),
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


def corroboration_scores(
    driver: Driver,
    asset_id: str,
    *,
    scope_hops: int = 4,
) -> dict[str, float]:
    """스코프 노드별 '독립 출처(언론사) 수' — 여러 출처가 보강할수록 강한 노드.

    SemanticPolicy의 corroboration 입력. Fact 총량(에코·재탕 포함)이 아니라 그
    Fact들이 추출된 WebDoc의 distinct press 수를 센다 — 같은 보도자료 재탕은 1로.
    **자산 스코프 내 Fact만** 센다 — 타 자산이 전역 층에 쌓은 보강(예: 강남역 22)이
    무관한 이 자산에 새지 않도록(스코프 밖 Fact 제외). 즉 '이 자산 평가가 모은
    증거가 이 노드를 얼마나 보강했나'를 잰다.
    """
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (a:Asset {{asset_id: $aid}})-[*0..{int(scope_hops)}]-(f:Fact)
                  -[:MENTIONS]->(n)
            MATCH (f)-[:EXTRACTED_FROM]->(w:WebDoc)
            WITH n, count(DISTINCT coalesce(w.press, w.url)) AS press
            WHERE press > 0
            RETURN elementId(n) AS eid, press
            """,
            aid=asset_id,
        ).data()
    out = {r["eid"]: float(r["press"]) for r in rows}
    logger.info("corroboration_scores(%s): %d nodes with sources", asset_id, len(out))
    return out


def semantic_sims(
    driver: Driver,
    asset_id: str,
    purpose_text: str,
    *,
    scope_hops: int = 4,
) -> dict[str, float]:
    """자산 스코프 노드들의 card_embedding vs 목적 임베딩 코사인 → eid별 [0,1] 정규화.

    SemanticPolicy의 입력. walk 점수의 노드배수에 곱해져 '엣지 종류 상수'가 못 보는
    '이 노드가 목적과 의미적으로 가까운가'를 데이터로 반영한다 (방법1, LinearRAG의
    semantic bridging에 해당). e5 코사인은 스프레드가 좁아 스코프 내 min-max로
    정규화해 변별력을 준다. 결정적(임베딩·스코프 고정 → 같은 값) → 보고서에도 안전.
    """
    from infra.graph.embedder import embed_one

    pvec = embed_one(purpose_text)
    pnorm = sum(x * x for x in pvec) ** 0.5 or 1.0
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (a:Asset {{asset_id: $aid}})-[*0..{int(scope_hops)}]-(m)
            WHERE m.card_embedding IS NOT NULL
            RETURN DISTINCT elementId(m) AS eid, m.card_embedding AS emb
            """,
            aid=asset_id,
        ).data()
    sims: dict[str, float] = {}
    for r in rows:
        emb = r["emb"]
        if not emb:
            continue
        dot = sum(a * b for a, b in zip(pvec, emb))
        enorm = sum(x * x for x in emb) ** 0.5 or 1.0
        sims[r["eid"]] = dot / (pnorm * enorm)
    if sims:                                   # 스코프 내 min-max 정규화 (변별력)
        lo, hi = min(sims.values()), max(sims.values())
        rng = (hi - lo) or 1.0
        sims = {k: (v - lo) / rng for k, v in sims.items()}
    logger.info("semantic_sims(%s): %d nodes scored", purpose_text[:24], len(sims))
    return sims


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
    from infra.graph.embedder import embed_one
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
        in_scope = scope_eids(driver, scope_asset_id, scope_hops)
        ranked = [h for h in ranked if h["eid"] in in_scope]
    return [h["eid"] for h in ranked[:k]]
