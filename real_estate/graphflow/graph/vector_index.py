"""Neo4j 벡터 인덱스 — card_text를 임베딩해 card_embedding 프로퍼티로 저장.

Neo4j 5.13+ vector index로 ANN 검색. card_text가 결정적 템플릿이라 임베딩도
캐시 가능 (card_text가 안 바뀐 노드는 skip). 한 인덱스는 한 라벨만 보호하므로
라벨별 N개 생성한다.

QA 앵커 매칭(설계 §5.6)이 이 인덱스를 사용한다.
"""

from __future__ import annotations

import logging

from neo4j import Driver

from real_estate.graphflow.graph.embedder import dim, embed_texts
from real_estate.graphflow.graph.schema import NODE_LABELS

logger = logging.getLogger("graphflow.vector_index")

_BATCH = 64

# card_text를 갖는, QA 앵커가 될 수 있는 라벨. 노드가 없는 라벨의 인덱스는 무해.
INDEXED_LABELS = [
    NODE_LABELS.ASSET,
    NODE_LABELS.COMPLEX,
    NODE_LABELS.BUILDING,
    NODE_LABELS.CERTIFICATION,
    NODE_LABELS.COMMERCIAL_AREA,
    NODE_LABELS.INDUSTRY,
    NODE_LABELS.ADMIN_DONG,
    NODE_LABELS.LEGAL_DONG,
    NODE_LABELS.TRANSIT_STATION,
    NODE_LABELS.DEVELOPMENT_PROJECT,
    NODE_LABELS.TENANCY_EVENT,
    # 확장/보고서 단계에서 채워짐 (PR-C~)
    NODE_LABELS.FACT,
    NODE_LABELS.ENTITY,
    NODE_LABELS.WEB_DOC,
    NODE_LABELS.CLAIM,
    NODE_LABELS.REPORT_SECTION,
]


def _index_name(label: str) -> str:
    return f"vec_{label.lower()}_card"


def ensure_vector_index(driver: Driver) -> None:
    """라벨별 vector index 생성. idempotent."""
    with driver.session() as session:
        for label in INDEXED_LABELS:
            session.run(
                f"""
                CREATE VECTOR INDEX {_index_name(label)} IF NOT EXISTS
                FOR (n:{label}) ON (n.card_embedding)
                OPTIONS {{ indexConfig: {{
                    `vector.dimensions`: {dim()},
                    `vector.similarity_function`: 'cosine'
                }} }}
                """
            )
    logger.info("vector indexes ensured for %d labels", len(INDEXED_LABELS))


def update_embeddings_for_label(driver: Driver, label: str, *, overwrite: bool = False) -> int:
    """라벨의 card_text 보유 노드(또는 임베딩 없는 노드)에 임베딩 채움."""
    where = "WHERE n.card_text IS NOT NULL" + (
        "" if overwrite else " AND n.card_embedding IS NULL"
    )
    with driver.session() as session:
        result = session.run(
            f"MATCH (n:{label}) {where} RETURN elementId(n) AS eid, n.card_text AS card"
        )
        items = [(r["eid"], r["card"]) for r in result]

    if not items:
        return 0

    written = 0
    for i in range(0, len(items), _BATCH):
        chunk = items[i : i + _BATCH]
        vectors = embed_texts([card for _, card in chunk])
        with driver.session() as session:
            session.run(
                """
                UNWIND $rows AS row
                MATCH (n) WHERE elementId(n) = row.eid
                CALL db.create.setNodeVectorProperty(n, 'card_embedding', row.vec)
                """,
                rows=[{"eid": eid, "vec": vec}
                      for (eid, _), vec in zip(chunk, vectors)],
            )
        written += len(chunk)
    logger.info("embeddings updated for %s: %d", label, written)
    return written


def update_all_embeddings(driver: Driver, *, overwrite: bool = False) -> dict:
    return {
        label: update_embeddings_for_label(driver, label, overwrite=overwrite)
        for label in INDEXED_LABELS
    }


def search_similar(driver: Driver, label: str, query_vector: list[float], *, k: int = 10) -> list[dict]:
    """라벨별 vector index 검색. (eid, card, labels, score) 반환."""
    with driver.session() as session:
        result = session.run(
            f"""
            CALL db.index.vector.queryNodes('{_index_name(label)}', $k, $vec)
            YIELD node, score
            RETURN elementId(node) AS eid, node.card_text AS card,
                   labels(node) AS labels, score
            """,
            k=k, vec=query_vector,
        )
        return [dict(r) for r in result]
