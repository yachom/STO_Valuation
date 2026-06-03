"""NaiveNeighborhoodRetriever — Retriever 프로토콜의 1차 구현.

전략:
    1. 질의 임베딩 (bge-m3)
    2. axis에 매핑된 라벨별로 vector index 검색 → top-K 노드
    3. 각 hit에 대해 1-hop 이웃 노드의 card_text도 합쳐 GroundedSnippet 구성
    4. 점수 순 정렬해 k건 반환

이 구현은 community detection 없이도 의미 검색 + 그래프 구조를 함께 활용한다.
향후 HippoRAG/MS GraphRAG로 swap 가능 (Retriever 프로토콜만 준수).
"""

from __future__ import annotations

import logging

from neo4j import Driver

from real_estate.report.graph.schema import NODE_LABELS
from real_estate.report.rag.embedder import embed_one
from real_estate.report.rag.protocol import Axis, GroundedSnippet, Retriever
from real_estate.report.rag.vector_index import search_similar

logger = logging.getLogger("rag.naive")


# 축별 후보 라벨. 한 라벨이 여러 축에 포함되어도 무방.
_AXIS_LABELS: dict[Axis, list[str]] = {
    "tenant": [
        NODE_LABELS.COMPLEX,
        NODE_LABELS.TENANCY_EVENT,
    ],
    "esg": [
        NODE_LABELS.BUILDING,
        NODE_LABELS.CERTIFICATION,
        NODE_LABELS.COMPLEX,
    ],
    "commercial": [
        NODE_LABELS.COMMERCIAL_AREA,
        NODE_LABELS.DEVELOPMENT_PROJECT,
        NODE_LABELS.COMPLEX,
    ],
}

_ALL_INDEXED_LABELS = [
    NODE_LABELS.COMPLEX,
    NODE_LABELS.BUILDING,
    NODE_LABELS.CERTIFICATION,
    NODE_LABELS.TENANCY_EVENT,
    NODE_LABELS.COMMERCIAL_AREA,
    NODE_LABELS.DEVELOPMENT_PROJECT,
]


class NaiveNeighborhoodRetriever(Retriever):
    """vector index hit + 1-hop subgraph 텍스트 결합."""

    def __init__(
        self,
        driver: Driver,
        *,
        per_label_k: int = 5,
        neighbor_hops: int = 1,
        neighbor_limit: int = 8,
    ):
        self._driver = driver
        self._per_label_k = per_label_k
        self._neighbor_hops = neighbor_hops
        self._neighbor_limit = neighbor_limit

    def retrieve(
        self,
        query: str,
        *,
        k: int,
        axis: Axis | None = None,
    ) -> list[GroundedSnippet]:
        vec = embed_one(query)
        labels = _AXIS_LABELS[axis] if axis else _ALL_INDEXED_LABELS

        # 라벨별 vector 검색 → 합집합
        hits: list[dict] = []
        for label in labels:
            hits.extend(search_similar(
                self._driver, label, vec, k=self._per_label_k,
            ))
        # 점수 내림차순 + 중복 제거(같은 eid 1개만 유지)
        seen: set[str] = set()
        unique: list[dict] = []
        for h in sorted(hits, key=lambda r: r["score"], reverse=True):
            if h["eid"] in seen:
                continue
            seen.add(h["eid"])
            unique.append(h)

        # 상위 k에 대해 1-hop 이웃 카드 끌어와 텍스트 결합
        top = unique[:k]
        snippets: list[GroundedSnippet] = []
        with self._driver.session() as session:
            for hit in top:
                eid = hit["eid"]
                primary_card = hit["card"] or ""
                primary_labels = hit["labels"] or []

                neighbors_result = session.run(
                    f"""
                    MATCH (n) WHERE elementId(n) = $eid
                    OPTIONAL MATCH (n)-[r*1..{self._neighbor_hops}]-(m)
                    WHERE m.card_text IS NOT NULL
                    WITH n, collect(DISTINCT m)[0..$nb] AS neighbors,
                         collect(DISTINCT r) AS rels
                    RETURN
                      [x IN neighbors | elementId(x)] AS neighbor_eids,
                      [x IN neighbors | x.card_text]  AS neighbor_cards,
                      [x IN neighbors | labels(x)]    AS neighbor_labels
                    """,
                    eid=eid,
                    nb=self._neighbor_limit,
                )
                row = neighbors_result.single()
                neighbor_eids = row["neighbor_eids"] if row else []
                neighbor_cards = row["neighbor_cards"] if row else []

                parts = [primary_card] + [c for c in neighbor_cards if c]
                text = "\n".join(parts)

                snippet = GroundedSnippet(
                    text=text,
                    node_refs=[eid] + list(neighbor_eids),
                    edge_refs=[],
                    sqlite_refs=_eid_to_sqlite_refs(eid, primary_labels),
                    score=float(hit["score"]),
                    metadata={
                        "primary_labels": primary_labels,
                        "neighbor_count": len(neighbor_eids),
                    },
                )
                snippets.append(snippet)
        return snippets


def _eid_to_sqlite_refs(eid: str, labels: list[str]) -> list[tuple[str, str]]:
    """elementId 자체는 SQLite PK가 아니지만, primary_labels로부터 매핑 가능한
    경우 (table, pk_field) hint만 제공. 구체 PK 값은 노드의 자연 키 프로퍼티에서
    fetch해야 하므로 호출자가 필요 시 다시 조회.

    v1에선 metadata만 제공하고 빈 리스트로 둠 (citation은 node_refs로 충분).
    """
    return []
