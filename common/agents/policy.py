"""PolicyGate — enforces per-scope budget on agent actions.

Inputs come from observability.cost (accumulated USD) and per-scope counters.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Budget(BaseModel):
    max_tool_calls: int = 8
    max_cost_usd: float = 1.0
    allowed_tools: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    allow: bool
    reason: str = ""


class PolicyGate:
    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.tool_calls = 0

    def check(self, tool_name: str, *, scope: str | None = None) -> Decision:
        raise NotImplementedError

    def record_call(self, tool_name: str) -> None:
        raise NotImplementedError
