"""Graph storage port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from common.domain.graph import GraphEdge, GraphNode


@runtime_checkable
class GraphWriter(Protocol):
    def upsert_node(self, node: GraphNode) -> None: ...

    def upsert_edge(self, edge: GraphEdge) -> None: ...

    def upsert_many(
        self, nodes: list[GraphNode], edges: list[GraphEdge]
    ) -> None: ...


@runtime_checkable
class GraphReader(Protocol):
    def get_node(self, node_id: str) -> GraphNode | None: ...

    def neighbors(
        self, node_id: str, relation: str | None = None
    ) -> list[GraphNode]: ...
