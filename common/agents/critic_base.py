"""Critic — judges whether the collected evidence is sufficient."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from common.agents.base_agent import BaseAgent


class Verdict(BaseModel):
    stop: bool
    reason: str = ""


class CriticBase(BaseAgent):
    def run(self, state: Any) -> Verdict:
        raise NotImplementedError
