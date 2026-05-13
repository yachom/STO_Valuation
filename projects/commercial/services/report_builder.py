"""Commercial report builder — graph traversal → markdown report."""

from __future__ import annotations

from common.ports.graph import GraphReader
from projects.commercial.domain.claims import AnalysisResult


class CommercialReportBuilder:
    def __init__(self, graph: GraphReader) -> None:
        self._graph = graph

    def build(self, *, scope_id: str, target_id: str) -> AnalysisResult:
        raise NotImplementedError
