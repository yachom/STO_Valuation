"""Exploration LangGraph: Planner → PolicyGate → Retriever → Critic loop.

Assets compose this builder with their own planner/retriever/critic instances.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from common.agents.critic_base import CriticBase
from common.agents.planner_base import PlannerBase
from common.agents.policy import PolicyGate
from common.agents.retriever_base import RetrieverBase, ToolResult


class ExplorationState(BaseModel):
    asset_class: str
    target_id: str
    iteration: int = 0
    max_iterations: int = 3
    history: list[ToolResult] = Field(default_factory=list)
    scratch: dict[str, Any] = Field(default_factory=dict)


def build_exploration_graph(
    *,
    planner: PlannerBase,
    retriever: RetrieverBase,
    critic: CriticBase,
    gate: PolicyGate,
    max_iterations: int = 3,
) -> Any:
    """Return a compiled LangGraph runnable."""
    raise NotImplementedError
