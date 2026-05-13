"""Planner — decides the next tool call + rationale."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from common.agents.base_agent import BaseAgent


class PlannedAction(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class PlannerBase(BaseAgent):
    def run(self, state: Any) -> PlannedAction:
        raise NotImplementedError
