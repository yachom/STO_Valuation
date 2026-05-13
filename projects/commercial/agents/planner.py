"""Commercial Planner — extends PlannerBase with asset prompt."""

from __future__ import annotations

from typing import Any

from common.agents.planner_base import PlannedAction, PlannerBase


class CommercialPlanner(PlannerBase):
    name = "commercial_planner"
    system_prompt = (
        "You plan information-gathering actions for valuing a Korean commercial "
        "real-estate building. Choose the next tool call from the available set, "
        "and explain why in one short sentence."
    )

    def run(self, state: Any) -> PlannedAction:
        raise NotImplementedError
