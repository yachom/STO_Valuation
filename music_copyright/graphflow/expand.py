"""EXPAND 루프 (음악저작권) — navigator→harvester→curator 라운드. real_estate 미러.

웹검색 주도 데이터 확장. 매 라운드: 프론티어 수집 → Navigator 검색계획 → Query 적재
→ Harvester 검색·Fact 추출·적재 → Curator 임베딩·novelty. novelty<임계 또는 예산 소진
또는 프론티어 없음이면 중단.

라운드 간은 순차다(프론티어가 직전 라운드 적재에 의존). 라운드 내 병목인 문서별
LLM 추출은 harvester가 스레드풀로 병렬화한다(harvester._parallel_ingest). openai
호출은 게이트웨이가 전역 throttle하므로 동시 호출이 많아도 429가 없다.
"""

from __future__ import annotations

import logging

from neo4j import Driver

from music_copyright.graphflow.agents.base import normalize_text, short_hash
from music_copyright.graphflow.curator import curate_round, fetch_fact_embeddings
from music_copyright.graphflow.graph.queries import work_card_text
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.harvester import Harvester
from music_copyright.graphflow.navigator import Navigator, collect_frontier
from music_copyright.graphflow.traversal import scope_eids

logger = logging.getLogger("music_copyright.expand")


def _executed_queries(driver: Driver, work_id: str, scope_hops: int) -> list[str]:
    """이미 실행한 검색어(중복 방지) — 스코프 내 Query 노드 text."""
    eids = scope_eids(driver, work_id, scope_hops)
    if not eids:
        return []
    with driver.session() as s:
        rows = s.run(
            f"UNWIND $eids AS eid MATCH (n) WHERE elementId(n)=eid "
            f"MATCH (n)-[:{E.SPAWNED_QUERY}]->(q:{L.QUERY}) RETURN DISTINCT q.text AS t",
            eids=list(eids),
        ).data()
    return [r["t"] for r in rows if r["t"]]


def _write_queries(driver: Driver, plans) -> dict[str, str]:
    """Query 노드 + (target)-SPAWNED_QUERY->(Query) 적재. 반환 {query_text: query_hash}."""
    qk = KEY_PROP[L.QUERY]
    out: dict[str, str] = {}
    with driver.session() as s:
        for plan in plans:
            t_label, _, t_key = plan.target.partition(":")
            for q in plan.queries:
                qhash = short_hash(normalize_text(q))
                out[q] = qhash
                s.run(f"MERGE (x:{L.QUERY} {{{qk}: $h}}) SET x.text = $t, x.card_text = $t",
                      h=qhash, t=q)
                if t_key:
                    tk = KEY_PROP[t_label]
                    s.run(
                        f"MATCH (n:{t_label} {{{tk}: $tkey}}) MATCH (q:{L.QUERY} {{{qk}: $h}}) "
                        f"MERGE (n)-[:{E.SPAWNED_QUERY}]->(q)", tkey=t_key, h=qhash)
    return out


def run_expand(
    driver: Driver,
    *,
    work_id: str,
    collected_by: str,
    rounds: int | None = None,
    frontier_max: int | None = None,
    queries_per_node: int | None = None,
    total_query_budget: int | None = None,
    novelty_threshold: float | None = None,
    scope_hops: int = 3,
    purpose: str | None = None,
) -> dict:
    """웹검색 EXPAND 실행. 기본값은 config music_graphflow `expand`에서."""
    from music_copyright.graphflow.config import get_config
    e = get_config()["expand"]
    rounds = e["rounds"] if rounds is None else rounds
    frontier_max = e["frontier_max"] if frontier_max is None else frontier_max
    queries_per_node = e["queries_per_node"] if queries_per_node is None else queries_per_node
    total_query_budget = e["total_query_budget"] if total_query_budget is None else total_query_budget
    novelty_threshold = e["novelty_threshold"] if novelty_threshold is None else novelty_threshold

    work_card = work_card_text(driver, work_id)
    navigator, harvester = Navigator(), Harvester()
    prior_vecs = fetch_fact_embeddings(driver)
    queries_spent = 0
    history: list[dict] = []

    for rnd in range(1, rounds + 1):
        if queries_spent >= total_query_budget:
            break
        frontier = collect_frontier(driver, work_id, limit=frontier_max)
        if not frontier:
            history.append({"round": rnd, "reason": "no_frontier"})
            break
        executed = _executed_queries(driver, work_id, scope_hops)
        plans = navigator.plan_round(
            work_card, frontier, executed,
            max_targets=frontier_max, queries_per_node=queries_per_node, purpose=purpose)
        # 예산 trim
        trimmed, n_q = [], 0
        for plan in plans:
            remaining = total_query_budget - queries_spent - n_q
            if remaining <= 0:
                break
            qs = plan.queries[:remaining]
            if qs:
                plan.queries = qs
                trimmed.append(plan)
                n_q += len(qs)
        if not trimmed:
            history.append({"round": rnd, "reason": "no_queries"})
            break
        query_hashes = _write_queries(driver, trimmed)
        queries_spent += n_q

        target_cards = {c.ref: c.card for c in frontier}
        stats = harvester.harvest(
            driver, work_id=work_id, work_card=work_card, plans=trimmed,
            query_hashes=query_hashes, target_cards=target_cards,
            collected_by=collected_by, scope_hops=scope_hops)

        novelty = curate_round(driver, new_fact_texts=stats.new_fact_texts,
                               prior_fact_vecs=prior_vecs)
        history.append({
            "round": rnd, "targets": len(trimmed), "queries": n_q,
            "docs_new": stats.docs_new, "facts_new": stats.facts_new,
            "entities_new": stats.entities_new,
            "mentions_confirmed": stats.mentions_confirmed,
            "novelty": round(novelty, 3),
        })
        if novelty < novelty_threshold:
            break

    summary = {"work_id": work_id, "rounds_run": len(history),
               "queries_total": queries_spent, "history": history}
    logger.info("run_expand DONE: %s", summary)
    return summary
