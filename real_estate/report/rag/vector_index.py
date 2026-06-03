"""Neo4j 벡터 인덱스 — bge-m3로 card_text를 임베딩, card_embedding 프로퍼티로 저장.

Neo4j 5.x는 노드 프로퍼티에 list<float>을 두고 vector index를 만들면 ANN 검색이
가능하다. 카드 텍스트가 이미 결정적 템플릿으로 채워져 있으므로 임베딩도 캐시
가능 (card_text 내용이 바뀌지 않은 노드는 skip).
"""

from __future__ import annotations

import logging

from neo4j import Driver

from real_estate.report.graph.schema import NODE_LABELS
from real_estate.report.rag.embedder import dim, embed_texts

logger = logging.getLogger("rag.vector_index")

_BATCH = 64
INDEX_NAME = "card_text_index"


# 어떤 라벨이 벡터 검색 대상인지. card_text가 있는 라벨만.
_INDEXED_LABELS = [
    NODE_LABELS.COMPLEX,
    NODE_LABELS.BUILDING,
    NODE_LABELS.CERTIFICATION,
    NODE_LABELS.TENANCY_EVENT,
    NODE_LABELS.COMMERCIAL_AREA,
    NODE_LABELS.DEVELOPMENT_PROJECT,
]


def ensure_vector_index(driver: Driver) -> None:
    """라벨별 vector index 생성. Neo4j 5.13+ 필요.

    한 인덱스는 한 라벨만 보호하므로 라벨별로 N개 생성한다.
    """
    with driver.session() as session:
        for label in _INDEXED_LABELS:
            idx_name = f"vec_{label.lower()}_card"
            session.run(
                f"""
                CREATE VECTOR INDEX {idx_name} IF NOT EXISTS
                FOR (n:{label}) ON (n.card_embedding)
                OPTIONS {{ indexConfig: {{
                    `vector.dimensions`: {dim()},
                    `vector.similarity_function`: 'cosine'
                }} }}
                """
            )
    logger.info("vector indexes ensured for %d labels", len(_INDEXED_LABELS))


def update_embeddings_for_label(
    driver: Driver,
    label: str,
    *,
    overwrite: bool = False,
) -> int:
    """라벨의 모든 노드(또는 card_embedding 없는 노드)에 임베딩 채움."""
    where = "WHERE n.card_text IS NOT NULL" + (
        "" if overwrite else " AND n.card_embedding IS NULL"
    )
    with driver.session() as session:
        result = session.run(
            f"MATCH (n:{label}) {where} "
            f"RETURN elementId(n) AS eid, n.card_text AS card"
        )
        items = [(r["eid"], r["card"]) for r in result]

    if not items:
        return 0

    written = 0
    for i in range(0, len(items), _BATCH):
        chunk = items[i : i + _BATCH]
        eids = [eid for eid, _ in chunk]
        cards = [card for _, card in chunk]
        vectors = embed_texts(cards)

        with driver.session() as session:
            session.run(
                """
                UNWIND $rows AS row
                MATCH (n) WHERE elementId(n) = row.eid
                CALL db.create.setNodeVectorProperty(n, 'card_embedding', row.vec)
                """,
                rows=[{"eid": eid, "vec": vec} for eid, vec in zip(eids, vectors)],
            )
            written += len(chunk)
    logger.info("embeddings updated for %s: %d", label, written)
    return written


def update_all_embeddings(driver: Driver, *, overwrite: bool = False) -> dict:
    return {
        label: update_embeddings_for_label(driver, label, overwrite=overwrite)
        for label in _INDEXED_LABELS
    }


def search_similar(
    driver: Driver,
    label: str,
    query_vector: list[float],
    *,
    k: int = 10,
) -> list[dict]:
    """라벨별 vector index 검색. (elementId, score, card_text) 반환."""
    idx_name = f"vec_{label.lower()}_card"
    with driver.session() as session:
        result = session.run(
            f"""
            CALL db.index.vector.queryNodes('{idx_name}', $k, $vec)
            YIELD node, score
            RETURN elementId(node) AS eid,
                   node.card_text AS card,
                   labels(node) AS labels,
                   score
            """,
            k=k,
            vec=query_vector,
        )
        return [dict(r) for r in result]
