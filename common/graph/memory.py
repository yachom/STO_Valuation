"""In-memory graph store."""

from __future__ import annotations

from common.domain.graph import GraphEdge, GraphNode
from common.graph.base import BaseGraphStore


class MemoryGraphStore(BaseGraphStore):
    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []

    def upsert_node(self, node: GraphNode) -> None:
        raise NotImplementedError

    def upsert_edge(self, edge: GraphEdge) -> None:
        raise NotImplementedError

    def upsert_many(self, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        raise NotImplementedError

    def get_node(self, node_id: str) -> GraphNode | None:
        raise NotImplementedError

    def neighbors(self, node_id: str, relation: str | None = None) -> list[GraphNode]:
        raise NotImplementedError
