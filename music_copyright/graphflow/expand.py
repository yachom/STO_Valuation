"""EXPAND 루프 (음악저작권) — navigator→harvester→curator 라운드. real_estate 미러.

웹검색 주도 데이터 확장. 매 라운드: 프론티어 수집 → Navigator 검색계획 → Query 적재
→ Harvester 검색·Fact 추출·적재 → Curator 임베딩·novelty. novelty<임계 또는 예산 소진
또는 프론티어 없음이면 중단.

라운드 간은 순차다(프론티어가 직전 라운드 적재에 의존). 라운드 내 병목인 문서별
LLM 추출은 harvester가 스레드풀로 병렬화한다(harvester._parallel_ingest). openai
호출은 게이트웨이가 전역 throttle하므로 동시 호출이 많아도 429가 없다.
"""

from __future__ import annotations

import json
import logging

from neo4j import Driver
from infra.agent.tracing import emit

from music_copyright.graphflow.agents.base import normalize_text, short_hash
from music_copyright.graphflow.coverage import axis_coverage, coverage_summary_text
from music_copyright.graphflow.curator import curate_round, fetch_fact_embeddings
from music_copyright.graphflow.graph.queries import work_card_text
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.harvester import Harvester
from music_copyright.graphflow.navigator import Navigator, collect_frontier
from music_copyright.graphflow.research import QueryOutcome
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


def _stored_query_outcomes(driver: Driver, work_id: str, *, limit: int = 40) -> list[QueryOutcome]:
    """Reload feedback across separate run_expand calls used by STORM researchers."""
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (w:{L.WORK} {{{KEY_PROP[L.WORK]}: $wid}})
                  -[wr:{E.SPAWNED_QUERY}]->(q:{L.QUERY})
            WHERE q.utility IS NOT NULL
            RETURN q.task_id AS task_id, q.axis AS axis, q.text AS query,
                   q.strategy AS strategy, q.channels AS channels,
                   q.docs_selected AS docs_selected, q.docs_ingested AS docs_ingested,
                   q.docs_new AS docs_new, q.grounded_facts AS grounded_facts,
                   q.facts_new AS facts_new, q.entities_new AS entities_new,
                   q.mentions_confirmed AS mentions_confirmed,
                   q.outcome_sources AS sources, q.skip_reasons_json AS skip_reasons,
                   coalesce(q.outcome_recorded_at, datetime({{epochMillis: 0}})) AS recorded_at,
                   q.evidence_type AS evidence_type, wr.target_ref AS target_ref
            ORDER BY recorded_at DESC
            LIMIT $limit
            """,
            wid=work_id, limit=limit,
        ).data()
    out = []
    for r in reversed(rows):
        try:
            skipped = json.loads(r.get("skip_reasons") or "{}")
        except (TypeError, ValueError):
            skipped = {}
        out.append(QueryOutcome(
            task_id=r.get("task_id") or "",
            target=r.get("target_ref") or f"{L.WORK}:{work_id}",
            axis=r.get("axis") or "cross_axis",
            query=r.get("query") or "",
            strategy=r.get("strategy") or "discovery",
            channels=list(r.get("channels") or []),
            docs_selected=int(r.get("docs_selected") or 0),
            docs_ingested=int(r.get("docs_ingested") or 0),
            docs_new=int(r.get("docs_new") or 0),
            grounded_facts=int(r.get("grounded_facts") or 0),
            facts_new=int(r.get("facts_new") or 0),
            entities_new=int(r.get("entities_new") or 0),
            mentions_confirmed=int(r.get("mentions_confirmed") or 0),
            skip_reasons=skipped,
            sources=set(r.get("sources") or []),
        ))
    return out


def _write_queries(driver: Driver, work_id: str, plans, *, round_no: int,
                   collected_by: str) -> dict[str, str]:
    """Persist research provenance and scope every query to the evaluated Work."""
    qk = KEY_PROP[L.QUERY]
    out: dict[str, str] = {}
    with driver.session() as s:
        for plan in plans:
            t_label, _, t_key = plan.target.partition(":")
            for variant in plan.query_variants():
                q = variant.text
                qhash = short_hash(f"{work_id}|{plan.target}|{normalize_text(q)}")
                out[q] = qhash
                s.run(
                    f"""
                    MERGE (x:{L.QUERY} {{{qk}: $h}})
                    SET x.text = $t, x.card_text = $card, x.task_id = $task_id,
                        x.axis = $axis, x.question = $question,
                        x.evidence_goal = $goal, x.evidence_type = $etype,
                        x.strategy = $strategy, x.channels = $channels,
                        x.round = $round, x.collected_by = $collected_by
                    """,
                    h=qhash, t=q,
                    card=(f"조사과제 [{plan.axis}/{plan.evidence_type}] "
                          f"{plan.question} 검색어: {q}"),
                    task_id=plan.task_id, axis=plan.axis,
                    question=plan.question, goal=plan.evidence_goal,
                    etype=plan.evidence_type, strategy=variant.strategy,
                    channels=list(variant.channels), round=round_no,
                    collected_by=collected_by,
                )
                # Artist/Entity 조사를 포함해 모든 결과를 이번 Work 평가 corpus에 귀속한다.
                s.run(
                    f"""
                    MATCH (w:{L.WORK} {{{KEY_PROP[L.WORK]}: $wid}})
                    MATCH (q:{L.QUERY} {{{qk}: $h}})
                    MERGE (w)-[r:{E.SPAWNED_QUERY}]->(q)
                    SET r.target_ref = $target, r.task_id = $task_id, r.round = $round
                    """,
                    wid=work_id, h=qhash, target=plan.target,
                    task_id=plan.task_id, round=round_no,
                )
                if t_key:
                    tk = KEY_PROP[t_label]
                    s.run(
                        f"MATCH (n:{t_label} {{{tk}: $tkey}}) "
                        f"MATCH (q:{L.QUERY} {{{qk}: $h}}) "
                        f"MERGE (n)-[r:{E.SPAWNED_QUERY}]->(q) "
                        "SET r.task_id = $task_id, r.round = $round",
                        tkey=t_key, h=qhash, task_id=plan.task_id, round=round_no)
    return out


def _collection_query_count(driver: Driver, collected_by: str) -> int:
    with driver.session() as s:
        row = s.run(
            f"MATCH (q:{L.QUERY} {{collected_by: $collected_by}}) RETURN count(q) AS n",
            collected_by=collected_by,
        ).single()
    return int(row["n"] if row else 0)


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
    global_query_budget: int | None = None,
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
    prior_vecs = fetch_fact_embeddings(driver, work_id=work_id)
    queries_spent = 0
    history: list[dict] = []
    feedback = _stored_query_outcomes(driver, work_id)
    poor_rounds = 0
    min_query_utility = float(e.get("min_query_utility", 0.18))
    max_poor_rounds = int(e.get("max_poor_rounds", 2))

    for rnd in range(1, rounds + 1):
        if queries_spent >= total_query_budget:
            break
        global_spent = _collection_query_count(driver, collected_by)
        if global_query_budget is not None and global_spent >= global_query_budget:
            history.append({"round": rnd, "reason": "global_query_budget"})
            break
        frontier = collect_frontier(driver, work_id, limit=frontier_max,
                                    max_hops=frontier_hops)
        if not frontier:
            history.append({"round": rnd, "reason": "no_frontier"})
            break
        executed = _executed_queries(driver, work_id, scope_hops)
        cov_before = axis_coverage(driver, work_id)
        coverage_before = coverage_summary_text(cov_before)
        plans = navigator.plan_round(
            work_card, frontier, executed,
            max_targets=frontier_max, queries_per_node=queries_per_node, purpose=purpose,
            coverage=coverage_before, feedback=feedback, round_no=rnd)
        # 축별 예산 공정성 — 커버리지 낮은(Fact 적은) 축의 plan을 먼저 배분해, 한 축(매우 사회적인
        # 곡의 social_value 등)이 공유 예산을 독점하고 ESG 등이 굶는 것을 막는다(전체 예산만 있고
        # 축별 예산이 없던 문제). ESG floor가 만든 소속사(BigHit 등) 앵커 질의도 이 우선순위로
        # 예산을 확보해 연계주체 거버넌스가 실제로 조사된다. 안정 정렬이라 동커버 축은 순서 보존.
        plans = sorted(plans, key=lambda p: getattr(
            cov_before.get(getattr(p, "axis", ""), None), "n_facts", 0))
        # 예산 trim
        trimmed, n_q = [], 0
        for plan in plans:
            remaining = total_query_budget - queries_spent - n_q
            if global_query_budget is not None:
                remaining = min(remaining, global_query_budget - global_spent - n_q)
            if remaining <= 0:
                break
            variants = plan.query_variants()[:remaining]
            if variants:
                plan.variants = variants
                plan.queries = []
                trimmed.append(plan)
                n_q += len(variants)
        if not trimmed:
            history.append({"round": rnd, "reason": "no_queries"})
            break
        query_hashes = _write_queries(
            driver, work_id, trimmed, round_no=rnd, collected_by=collected_by)
        queries_spent += n_q

        target_cards = {c.ref: c.card for c in frontier}
        stats = harvester.harvest(
            driver, work_id=work_id, work_card=work_card, plans=trimmed,
            query_hashes=query_hashes, target_cards=target_cards,
            collected_by=collected_by, scope_hops=scope_hops)

        novelty = curate_round(driver, new_fact_texts=stats.new_fact_texts,
                               prior_fact_vecs=prior_vecs)
        feedback.extend(stats.outcomes)
        utilities = [o.utility for o in stats.outcomes]
        avg_utility = sum(utilities) / len(utilities) if utilities else 0.0
        if avg_utility < min_query_utility:
            poor_rounds += 1
        else:
            poor_rounds = 0
        coverage_after = coverage_summary_text(axis_coverage(driver, work_id))
        round_summary = {
            "round": rnd, "targets": len(trimmed), "queries": n_q,
            "docs_seen": stats.docs_seen, "docs_ingested": stats.docs_ingested,
            "docs_new": stats.docs_new, "grounded_facts": stats.grounded_facts,
            "facts_new": stats.facts_new,
            "entities_new": stats.entities_new,
            "mentions_confirmed": stats.mentions_confirmed,
            "novelty": round(novelty, 3),
            "query_utility": round(avg_utility, 3),
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "tasks": [p.model_dump(exclude={"queries"}) for p in trimmed],
            "outcomes": [o.as_dict() for o in stats.outcomes],
        }
        history.append(round_summary)
        emit("expand_round", **round_summary)
        # 다음 라운드 novelty는 직전 라운드까지 포함한 Work-local corpus와 비교한다.
        prior_vecs = fetch_fact_embeddings(driver, work_id=work_id)
        if novelty < novelty_threshold or poor_rounds >= max_poor_rounds:
            break

    summary = {"work_id": work_id, "rounds_run": len(history),
               "queries_total": queries_spent, "history": history}
    logger.info("run_expand DONE: %s", summary)
    return summary
