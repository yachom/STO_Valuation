"""BaseAgent — minimal contract for asset agents.

Subclasses (CommercialPlanner, MusicCritic, ...) override ``run`` and may
override prompts. The base wires LLM + tools + policy + tracing.
"""

from __future__ import annotations

from typing import Any

from common.agents.policy import PolicyGate
from common.ports.llm import LLMClient
from common.ports.tool import Tool


class BaseAgent:
    name: str = ""
    system_prompt: str = ""

    def __init__(
        self,
        llm: LLMClient,
        tools: list[Tool],
        policy: PolicyGate,
    ) -> None:
        self.llm = llm
        self.tools = {t.name: t for t in tools}
        self.policy = policy

    def run(self, state: Any) -> Any:
        raise NotImplementedError
