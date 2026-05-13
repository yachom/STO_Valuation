"""Commercial Retriever — executes a planned action via its tool set."""

from __future__ import annotations

from common.agents.planner_base import PlannedAction
from common.agents.retriever_base import RetrieverBase, ToolResult


class CommercialRetriever(RetrieverBase):
    name = "commercial_retriever"

    def run(self, action: PlannedAction) -> ToolResult:
        raise NotImplementedError
