"""Personalized PageRank 검색 — 손튜닝 엣지 상수 없는 멀티홉 (GraphRAG 2단계).

설계 §5의 weighted-BFS(StaticPolicy 상수표)를 대체하는 검색 프리미티브.
LinearRAG(arXiv 2510.10114)의 'relation-free + semantic bridging + PPR' 사상:

    ① 의미 시드: 각 스코프 노드의 card_text가 '축 의도'와 얼마나 가까운가(코사인)를
       personalization(restart) 분포로 둔다 → 의미 관련 노드에 질량 집중.
    ② PPR 전파: 위상(인접)만으로 random walk with restart → 멀티홉으로 증거 활성화.
       엣지 종류별 손가중 상수가 필요 없다(위상 = 고정 전이).
    ③ node specificity(HippoRAG 1/|연결| 유사): 허브 노드 자동 억제 → navigator
       라벨 상수(아파트 0.35) 같은 손튜닝을 원리로 대체.

손튜닝은 전역 2개(damping, anchor_weight)뿐 — 엣지별 14개 상수표가 사라진다.
결정적(임베딩·위상 고정). 점수는 PPR, 경로는 표시용으로 앵커에서의 최단경로를 붙인다.

⚠️ PoC 결론(2026-06-19, 마곡 검증): **보고서 증거 검색엔 부적합.** 본 그래프는
star형 — 고가치 curated 증거(TurnoverStat·Tenant·MarketContext)가 Asset에 1엣지로
붙은 degree-1 리프이고, 수집층(WebDoc/Fact)은 조밀히 연결. PPR은 연결성을 보상하므로
리프를 굶기고(우리 핵심 데이터 실종) 허브 WebDoc을 범람시킨다. semantic 블렌딩(β=0.7)
으로도 PPR≈0인 리프는 못 살린다. → 보고서는 SemanticPolicy(구조) + Fact 직접
벡터검색(뉴스) 하이브리드를 씀(composer). PPR은 entity-KG/passage 그래프(개방형 QA
멀티홉)용으로 보존 — "When to Use GraphRAG"(arXiv 2506.05690)의 task별 라우팅 결론과 일치.
"""

from __future__ import annotations

import logging
import math
from collections import deque

from neo4j import Driver

from real_estate.graphflow.graph.schema import KEY_PROP
from real_estate.graphflow.traversal import EdgeStep, EvidencePath, NodeRef, semantic_sims

logger = logging.getLogger("graphflow.ppr")

# 증거 후보에서 제외 — 보고서/QA의 *파생* 산출물(자기 과거 보고서를 증거로 인용하는
# 순환 방지). 1차 출처(Fact/통계/Tenant/Cert 등)만 증거가 된다.
_EXCLUDE_LABELS = ["Report", "ReportSection", "Claim", "Question", "Feedback", "Assessment"]


def _fetch_scope(driver: Driver, asset_id: str, hops: int):
    """스코프 노드 메타 + 내부 엣지. 임베딩은 semantic_sims가 따로 다룬다(경량).

    파생 산출물(보고서/QA 노드)은 제외 — 1차 출처만 증거 후보 + PPR 위상에서 배제.
    """
    with driver.session() as s:
        nrows = s.run(
            f"""
            MATCH (a:Asset {{asset_id: $aid}})-[*0..{int(hops)}]-(m)
            WITH DISTINCT m, labels(m)[0] AS lbl
            WHERE NOT lbl IN $exclude
            RETURN elementId(m) AS eid, lbl AS label,
                   coalesce(m.card_text, '') AS card,
                   m[$keymap[lbl]] AS key,
                   m.published_at AS published_at, m.valid_until AS valid_until,
                   m.temporal_kind AS temporal_kind, m.confidence AS confidence,
                   m.`앵커점수` AS anchor_score
            """,
            aid=asset_id, keymap=KEY_PROP, exclude=_EXCLUDE_LABELS,
        ).data()
        eids = [r["eid"] for r in nrows]
        erows = s.run(
            """
            MATCH (n)-[r]->(m)
            WHERE elementId(n) IN $eids AND elementId(m) IN $eids
            RETURN elementId(n) AS s, elementId(m) AS t, type(r) AS rel
            """,
            eids=eids,
        ).data()
    return nrows, erows


def ppr_evidence(
    driver: Driver,
    asset_id: str,
    purpose_text: str,
    *,
    k: int = 12,
    damping: float = 0.5,
    scope_hops: int = 3,
    anchor_weight: float = 0.15,
    use_specificity: bool = True,
    blend_semantic: float = 0.5,
    iters: int = 40,
    tol: float = 1e-9,
) -> list[EvidencePath]:
    """자산 앵커 + 축 의도 의미 시드에서 PPR → 상위 k 노드를 경로 붙여 반환.

    damping: restart 확률 = 1-damping (시드로 되돌아갈 확률). 0.5 = HippoRAG 기본.
    anchor_weight: 시드에서 앵커(자산)에 추가로 두는 질량(자산 grounding 유지).
    use_specificity: 허브 노드(고차수) 자동 억제 (PPR 점수 × 1/log(e+deg)).
    """
    from real_estate.graphflow.traversal import asset_anchor_eid

    anchor = asset_anchor_eid(driver, asset_id)
    if anchor is None:
        raise ValueError(f"Asset 노드 없음: {asset_id}")

    nrows, erows = _fetch_scope(driver, asset_id, scope_hops)
    if not nrows:
        return []
    meta = {r["eid"]: r for r in nrows}
    idx = {eid: i for i, eid in enumerate(meta)}
    n = len(idx)

    # 무향 인접 (walk가 양방향) + 경로 표시용 방향/관계 보존
    adj: list[list[int]] = [[] for _ in range(n)]
    nbr: dict[str, list[tuple[str, str, str]]] = {e: [] for e in meta}  # eid→(nbr,rel,dir)
    for r in erows:
        si, ti = idx.get(r["s"]), idx.get(r["t"])
        if si is None or ti is None:
            continue
        adj[si].append(ti)
        adj[ti].append(si)
        nbr[r["s"]].append((r["t"], r["rel"], "out"))
        nbr[r["t"]].append((r["s"], r["rel"], "in"))

    # ── ① 의미 시드 분포 (축 의도와의 코사인) + 앵커 질량 ──
    sims = semantic_sims(driver, asset_id, purpose_text, scope_hops=scope_hops)
    seed = [0.0] * n
    ssum = 0.0
    for eid, i in idx.items():
        v = sims.get(eid, 0.0)
        seed[i] = v
        ssum += v
    if ssum <= 0:
        for i in range(n):
            seed[i] = 1.0 / n
    else:
        for i in range(n):
            seed[i] /= ssum
    # 앵커에 추가 질량 → 재정규화 (자산 grounding)
    ai = idx[anchor]
    seed = [(1 - anchor_weight) * x for x in seed]
    seed[ai] += anchor_weight

    # ── ② PPR power iteration: r = (1-d)*seed + d * P^T r ──
    deg = [len(a) or 1 for a in adj]
    r = list(seed)
    for _ in range(iters):
        nr = [(1 - damping) * seed[i] for i in range(n)]
        for i in range(n):
            share = damping * r[i] / deg[i]
            if share == 0.0:
                continue
            for j in adj[i]:
                nr[j] += share
        diff = sum(abs(nr[i] - r[i]) for i in range(n))
        r = nr
        if diff < tol:
            break

    # ── ③ node specificity (허브 억제) ──
    if use_specificity:
        for i in range(n):
            r[i] *= 1.0 / math.log(math.e + deg[i])

    # ── ④ 의미 블렌딩 (HippoRAG2 dense+PPR): PPR 단독은 hub(WebDoc)에 쏠려 관련
    # 리프(TurnoverStat·Tenant)를 잃는다. PPR(위상·도달성)과 코사인(관련성)을
    # 기하 블렌드 → 둘 다 반영. blend_semantic=β. 정규화 후 (sim^β × ppr^(1-β)).
    rmax = max(r) or 1.0
    eps = 0.02
    blended: dict[str, float] = {}
    b = max(0.0, min(1.0, blend_semantic))
    for eid, i in idx.items():
        sim = sims.get(eid, 0.0)
        pn = r[i] / rmax
        blended[eid] = ((sim + eps) ** b) * ((pn + eps) ** (1.0 - b))

    # ── 상위 k (앵커 제외), 결정적 정렬 ──
    order = sorted(
        (e for e in meta if e != anchor),
        key=lambda e: (-blended[e], meta[e]["label"], str(meta[e]["key"])),
    )[:k]
    for eid in order:
        r[idx[eid]] = blended[eid]      # 표시 점수도 블렌드로

    # ── 경로(표시용): 앵커→노드 최단경로 BFS ──
    parent = _bfs_parents(anchor, nbr)
    out: list[EvidencePath] = []
    for eid in order:
        path = _reconstruct(anchor, eid, parent, meta)
        if path is None:
            nrf = _noderef(meta[eid])
            path = EvidencePath([_noderef(meta[anchor]), nrf], [], r[idx[eid]])
        path.score = r[idx[eid]]
        out.append(path)
    logger.info("ppr_evidence(%s): %d nodes → top %d", purpose_text[:24], n, len(out))
    return out


def _noderef(m: dict) -> NodeRef:
    return NodeRef(m["eid"], m["label"], str(m["key"]), m["card"])


def _bfs_parents(anchor: str, nbr: dict) -> dict:
    """앵커에서 BFS → eid → (parent_eid, rel, direction)."""
    parent = {anchor: None}
    q = deque([anchor])
    while q:
        cur = q.popleft()
        for nb_eid, rel, direction in nbr.get(cur, []):
            if nb_eid not in parent:
                parent[nb_eid] = (cur, rel, direction)
                q.append(nb_eid)
    return parent


def _reconstruct(anchor: str, target: str, parent: dict, meta: dict) -> EvidencePath | None:
    if target not in parent:
        return None
    chain = []
    cur = target
    while cur != anchor:
        p = parent[cur]
        if p is None:
            break
        prev, rel, direction = p
        chain.append((prev, rel, direction, cur))
        cur = prev
    chain.reverse()
    nodes = [_noderef(meta[anchor])]
    edges = []
    for prev, rel, direction, cur in chain:
        nodes.append(_noderef(meta[cur]))
        edges.append(EdgeStep(rel, direction, 1.0))
    return EvidencePath(nodes, edges, 0.0)
