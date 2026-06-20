"""EXPAND 루프 — 멀티 홉 그래프 기반 데이터 확장 오케스트레이션 (설계 §8).

라운드 구조:
    Navigator(frontier 선정+검색어, LLM 1회)
      → Harvester(검색→스크랩→추출→연결, 기사당 LLM 1회)
      → Curator(임베딩+novelty)
    novelty < 0.3 또는 예산 소진 시 조기 종료.

예산 (설계 §8.1): 라운드 ≤3, 라운드당 frontier ≤8, 노드당 검색어 ≤2, 총 검색 ≤40.
QA 라이브 확장(D7)은 이 함수를 축소 예산(rounds=1, frontier≤2, 총≤3)으로 호출한다.
"""

from __future__ import annotations

import logging

from neo4j import Driver

from real_estate.graphflow.curator import curate_round, fetch_fact_embeddings
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.harvester import Harvester
from real_estate.graphflow.navigator import (
    Navigator,
    collect_frontier,
    fetch_executed_queries,
    write_query_nodes,
)
from infra.agent.tracing import add_score, emit, node_scope

logger = logging.getLogger("graphflow.expand")

# 수집은 싸다(harvester/prefilter=gpt-mini) → 그래프를 살찌우는 쪽으로 공격적
# 수집. 멀티홉 표면화 정교화는 별개 과제. novelty 조기종료를 낮춰(0.3→0.1) 같은
# 얘기 재탕이 명확할 때만 멈추고, 라운드/예산 상한은 run_expand 기본값에서 상향.
NOVELTY_THRESHOLD = 0.1


def _asset_summary(driver: Driver, asset_id: str) -> tuple[str, str | None]:
    with driver.session() as s:
        r = s.run(
            f"MATCH (a:{L.ASSET} {{asset_id: $aid}}) "
            f"RETURN a.card_text AS card, a.sigungu AS sigungu",
            aid=asset_id,
        ).single()
        if r is None:
            raise ValueError(f"Asset 노드 없음: {asset_id} — Seeder 먼저 실행")
        return r["card"] or asset_id, r["sigungu"]


def run_expand(
    driver: Driver,
    *,
    asset_id: str,
    collected_by: str,
    rounds: int = 3,
    frontier_max: int = 12,
    queries_per_node: int = 3,
    total_query_budget: int = 100,
    novelty_threshold: float = NOVELTY_THRESHOLD,
    scope_hops: int = 3,
    purpose: str | None = None,    # QA 미니 EXPAND: 질문 구동 (§8.5, D7)
) -> dict:
    """EXPAND 실행. 반환: 라운드별 통계 요약."""
    asset_card, asset_sigungu = _asset_summary(driver, asset_id)
    navigator = Navigator()
    harvester = Harvester()

    history: list[dict] = []
    queries_spent = 0

    for rnd in range(1, rounds + 1):
        with node_scope(f"expand_round_{rnd}"):
            prior_vecs = fetch_fact_embeddings(driver)

            candidates = collect_frontier(
                driver, asset_id, max_hops=scope_hops - 1, limit=frontier_max + 4)
            executed = fetch_executed_queries(driver, asset_id, hops=scope_hops)
            if not candidates:
                emit("expand_stop", reason="no_frontier", round=rnd)
                break

            plans = navigator.plan_round(
                asset_card, candidates[:frontier_max], executed,
                max_targets=frontier_max, queries_per_node=queries_per_node,
                purpose=purpose)

            # 총 검색 예산 캡 — 초과분은 계획 뒤에서부터 잘라냄
            remaining = total_query_budget - queries_spent
            trimmed: list = []
            for plan in plans:
                if remaining <= 0:
                    break
                take = plan.queries[:remaining]
                remaining -= len(take)
                plan.queries = take
                trimmed.append(plan)
            plans = trimmed
            if not plans:
                emit("expand_stop", reason="budget_exhausted", round=rnd)
                break

            qhashes = write_query_nodes(
                driver, plans, candidates, collected_by=collected_by)
            queries_spent += len(qhashes)

            stats = harvester.harvest(
                driver,
                asset_id=asset_id, asset_card=asset_card,
                asset_sigungu=asset_sigungu,
                plans=plans, query_hashes=qhashes,
                target_cards={c.ref: c.card for c in candidates},
                collected_by=collected_by, scope_hops=scope_hops)

            novelty = curate_round(
                driver, new_fact_texts=stats.new_fact_texts,
                prior_fact_vecs=prior_vecs)

            row = {
                "round": rnd, "targets": len(plans), "queries": stats.queries,
                "docs_new": stats.docs_new, "facts_new": stats.facts_new,
                "entities_new": stats.entities_new,
                "mentions_confirmed": stats.mentions_confirmed,
                "mentions_candidate": stats.mentions_candidate,
                "novelty": round(novelty, 3),
            }
            history.append(row)
            emit("expand_round", **row)
            logger.info("EXPAND round %d: %s", rnd, row)

            if novelty < novelty_threshold:
                emit("expand_stop", reason="novelty_low", round=rnd, novelty=novelty)
                break

    # 엔티티 해소 — 수집한 Fact가 이름 약간 다른 스트레이 Entity로 새지 않게
    # 구조노드로 승격(Janitor §9.2/D15). corroboration의 전제: '발산역' Fact가
    # 발산역(마곡) 상권 노드를 강화하게. idempotent(merged_into).
    from real_estate.graphflow.janitor import run_region_promotions
    prom = run_region_promotions(driver)

    facts_total = sum(r["facts_new"] for r in history)
    add_score("expand_facts", facts_total)
    add_score("expand_rounds_run", len(history))
    add_score("entities_promoted", prom["promoted"])
    if history:
        add_score("expand_final_novelty", history[-1]["novelty"])
    return {"asset_id": asset_id, "rounds_run": len(history), "promotions": prom,
            "queries_total": queries_spent, "history": history}
