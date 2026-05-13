"""Router LLM — selects an underlying LLM per task hint (stub).

Implements LLMClient so it can be injected wherever a single LLM is expected.
"""

from __future__ import annotations

from typing import Any

from common.llm.base import BaseLLM
from common.ports.llm import Capability, LLMClient, LLMMessage, LLMResponse


class RouterLLM(BaseLLM):
    name = "router"
    capabilities: set[Capability] = set()

    def __init__(self, routes: dict[str, LLMClient], default: LLMClient) -> None:
        self._routes = routes
        self._default = default

    def pick(self, task_hint: str | None) -> LLMClient:
        raise NotImplementedError

    def generate(
        self,
        messages: list[LLMMessage],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        task_hint: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError
