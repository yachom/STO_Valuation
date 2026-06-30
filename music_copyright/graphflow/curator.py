"""Curator (음악저작권) — 라운드 후처리: 임베딩 + novelty. real_estate 미러.

신규 Fact/Entity/WebDoc 카드를 임베딩하고, 신규 Fact가 기존과 얼마나 다른지
(novelty = 코사인 유사도 < 임계 비율)를 재 EXPAND 중단 신호로 반환한다. LLM 없음.

⚠️ 임베더(sentence-transformers)가 없는 환경에서도 EXPAND가 죽지 않도록 임베딩은
best-effort다 — 실패 시 novelty=1.0(계속 진행, 라운드/예산 상한이 중단을 담당).
"""

from __future__ import annotations

import logging

from neo4j import Driver

from music_copyright.graphflow.graph.schema import NODE_LABELS as L

logger = logging.getLogger("music_copyright.curator")

_NOVELTY_SIM_THRESHOLD = 0.85


def fetch_fact_embeddings(driver: Driver, *, work_id: str | None = None,
                          scope_hops: int = 4) -> list[list[float]]:
    """Load novelty baselines, scoped to one Work when available."""
    try:
        if work_id:
            from music_copyright.graphflow.traversal import scope_eids
            eids = list(scope_eids(driver, work_id, scope_hops))
            if not eids:
                return []
            with driver.session() as s:
                rows = s.run(
                    f"UNWIND $eids AS eid MATCH (f:{L.FACT}) "
                    "WHERE elementId(f)=eid AND f.card_embedding IS NOT NULL "
                    "RETURN f.card_embedding AS v",
                    eids=eids,
                ).data()
            return [r["v"] for r in rows if r["v"]]
        with driver.session() as s:
            rows = s.run(
                f"MATCH (f:{L.FACT}) WHERE f.card_embedding IS NOT NULL "
                "RETURN f.card_embedding AS v",
            ).data()
        return [r["v"] for r in rows if r["v"]]
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_fact_embeddings skip: %s", exc)
        return []


def curate_round(
    driver: Driver, *, new_fact_texts: list[str], prior_fact_vecs: list[list[float]],
) -> float:
    """신규 노드 임베딩 + novelty 반환 (0~1). 임베더 실패는 1.0으로 흡수."""
    if not new_fact_texts:
        return 0.0
    try:
        from infra.graph.embedder import embed_texts
        from music_copyright.graphflow.graph.vector_index import (
            ensure_vector_index, update_embeddings_for_label)

        ensure_vector_index(driver)
        for label in (L.FACT, L.ENTITY, L.WEB_DOC):
            update_embeddings_for_label(driver, label)

        if not prior_fact_vecs:
            return 1.0
        new_vecs = embed_texts(new_fact_texts)
        novel = 0
        for nv in new_vecs:
            max_sim = max((sum(a * b for a, b in zip(nv, pv)) for pv in prior_fact_vecs),
                          default=0.0)
            if max_sim < _NOVELTY_SIM_THRESHOLD:
                novel += 1
        return novel / len(new_vecs)
    except Exception as exc:  # noqa: BLE001 — 임베더 부재 등
        logger.warning("curate 임베딩 실패(novelty=1.0로 계속): %s", exc)
        return 1.0
