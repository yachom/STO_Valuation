"""Retriever 프로토콜 — 에이전트와 그래프 RAG 구현 사이의 경계.

에이전트는 절대 구체 retriever를 import하지 않는다. `Retriever` Protocol에만
의존하여 향후 논문 기반 retriever (HippoRAG/StructRAG/MS GraphRAG 등)로 swap
가능하다. ([[tech_graph_stack]])

Protocol 시그니처:
    retrieve(query, *, k, axis=None) -> list[GroundedSnippet]

GroundedSnippet은 'evidence를 어디서 가져왔는지' (Neo4j 노드/엣지 + SQLite
원본 row)를 그대로 보존하여 validator/citation 로직이 추적 가능하게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


Axis = Literal["tenant", "esg", "commercial"]


@dataclass
class GroundedSnippet:
    """검색 결과 1건의 evidence 단위."""

    text: str
    """LLM 컨텍스트에 들어갈 자연어 텍스트 (보통 노드의 card_text + 이웃 요약)."""

    node_refs: list[str] = field(default_factory=list)
    """Neo4j elementId 리스트. citation 추적용."""

    edge_refs: list[str] = field(default_factory=list)
    """Neo4j elementId(엣지) 리스트."""

    sqlite_refs: list[tuple[str, str]] = field(default_factory=list)
    """(table_name, primary_key) tuple 리스트. raw row 추적용."""

    score: float = 0.0
    """retrieval 점수 (보통 cosine similarity 0~1, 높을수록 관련)."""

    metadata: dict = field(default_factory=dict)
    """retriever별 부가 정보."""


class Retriever(Protocol):
    """에이전트가 의존하는 단일 인터페이스."""

    def retrieve(
        self,
        query: str,
        *,
        k: int,
        axis: Axis | None = None,
    ) -> list[GroundedSnippet]:
        """질의 → 관련도 순 evidence 스니펫. axis가 주어지면 해당 축 노드로 제한."""
        ...
