"""Curator — 라운드 후처리: 신규 수집물 임베딩 + novelty 측정 (설계 §8.4).

PR-C 범위는 코드만:
    - 신규 Fact/Entity/WebDoc 카드 임베딩 → 벡터 인덱스 (QA 앵커 대비)
    - novelty = 신규 Fact 중 기존 Fact와 임베딩 유사도 < 0.85 비율
      (라운드 조기 종료 신호 — 더 파도 같은 얘기만 나오는지)
    - Entity dedup은 entity_id=정규화 표면형 MERGE로 자동 (동일 표면형 = 동일 노드)

LLM 기반 SAME_AS_CANDIDATE 승격 판정(Haiku)은 후보가 쌓인 뒤 후속 PR에서.
"""

from __future__ import annotations

import logging

from neo4j import Driver

from real_estate.graphflow.graph.embedder import embed_texts
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.graph.vector_index import (
    ensure_vector_index,
    update_embeddings_for_label,
)

logger = logging.getLogger("graphflow.curator")

_NOVELTY_SIM_THRESHOLD = 0.85
_COLLECTED_LABELS = (L.FACT, L.ENTITY, L.WEB_DOC)


def fetch_fact_embeddings(driver: Driver) -> list[list[float]]:
    """기존 Fact 노드들의 카드 임베딩 (novelty 비교 기준)."""
    with driver.session() as s:
        result = s.run(
            f"MATCH (f:{L.FACT}) WHERE f.card_embedding IS NOT NULL "
            f"RETURN f.card_embedding AS vec")
        return [r["vec"] for r in result]


def curate_round(
    driver: Driver,
    *,
    new_fact_texts: list[str],
    prior_fact_vecs: list[list[float]],
) -> float:
    """라운드 후처리. 반환: novelty (0~1).

    novelty는 '이번 라운드 신규 Fact 중, 라운드 시작 전 기존 Fact 어느 것과도
    유사도 0.85 미만인 것'의 비율. 기존 Fact가 없으면 신규가 있는 한 1.0.
    """
    ensure_vector_index(driver)
    for label in _COLLECTED_LABELS:
        update_embeddings_for_label(driver, label)

    if not new_fact_texts:
        return 0.0
    if not prior_fact_vecs:
        return 1.0

    new_vecs = embed_texts(new_fact_texts)   # normalize_embeddings=True → dot = cosine
    novel = 0
    for nv in new_vecs:
        max_sim = max(sum(a * b for a, b in zip(nv, pv)) for pv in prior_fact_vecs)
        if max_sim < _NOVELTY_SIM_THRESHOLD:
            novel += 1
    novelty = novel / len(new_vecs)
    logger.info("novelty %.2f (%d/%d 신규)", novelty, novel, len(new_vecs))
    return novelty
