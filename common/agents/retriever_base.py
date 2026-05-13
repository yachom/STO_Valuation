"""Retriever — executes a planned tool call."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from common.agents.base_agent import BaseAgent
from common.agents.planner_base import PlannedAction


class ToolResult(BaseModel):
    tool: str
    ok: bool
    data: Any = None
    error: str | None = None
    cost_usd: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrieverBase(BaseAgent):
    def run(self, action: PlannedAction) -> ToolResult:
        raise NotImplementedError
