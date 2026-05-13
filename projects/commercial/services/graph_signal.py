"""Commercial GraphSignalWriter — implements common.ports.news.GraphSignalWriter.

Maps a (news, ontology tags) pair into:
  - graph nodes for each non-NONE axis value
  - edges from News node to those nodes (GENERATES / AFFECTS / etc.)
  - a MarketSignal record persisted to the signal store
"""

from __future__ import annotations

from typing import Any

from common.domain.graph import GraphEdge, GraphNode
from common.ports.graph import GraphWriter
from common.ports.news import GraphSignalWriter
from common.ports.repository import NormalizedNews, SignalStore


class CommercialGraphSignalWriter(GraphSignalWriter):
    asset_class = "commercial"

    def __init__(self, graph: GraphWriter, signals: SignalStore) -> None:
        self._graph = graph
        self._signals = signals

    def write(self, news: NormalizedNews, tags: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def _build_nodes(
        self, news: NormalizedNews, tags: list[dict[str, Any]]
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        raise NotImplementedError
