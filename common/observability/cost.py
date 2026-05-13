"""Per-scope cost accumulator (LLM + paid API).

PolicyGate reads ``get(scope)`` to enforce ``max_cost``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CostEntry(BaseModel):
    source: str  # "anthropic:claude-opus-4-7", "openai:gpt-4o-mini", "vworld", ...
    usd: float
    metadata: dict[str, str] = Field(default_factory=dict)


def add(source: str, usd: float, **metadata: str) -> None:
    """Add cost to current scope. Uses observability.context.scope_id."""
    raise NotImplementedError


def get(scope: str | None = None) -> float:
    """Total USD for the given scope (defaults to current scope)."""
    raise NotImplementedError


def entries(scope: str | None = None) -> list[CostEntry]:
    raise NotImplementedError


def reset(scope: str | None = None) -> None:
    raise NotImplementedError
