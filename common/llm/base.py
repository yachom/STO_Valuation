"""Base LLM.

Owns:
  - retry / timeout
  - response cache (optional)
  - Langfuse generation recording
  - cost accumulation (observability.cost)

Subclasses implement ``_call_provider``. They MUST NOT touch services/agents.
"""

from __future__ import annotations

from typing import Any

from common.ports.llm import Capability, LLMMessage, LLMResponse


class BaseLLM:
    name: str = ""
    capabilities: set[Capability] = set()
    model_id: str = ""
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0

    def __init__(self, *, model_id: str | None = None, api_key: str | None = None) -> None:
        if model_id:
            self.model_id = model_id
        self.api_key = api_key

    def generate(
        self,
        messages: list[LLMMessage],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Public entrypoint — handles trace/cost; delegates to _call_provider."""
        raise NotImplementedError

    def _call_provider(
        self,
        messages: list[LLMMessage],
        *,
        json_schema: dict[str, Any] | None,
        temperature: float,
        max_tokens: int | None,
    ) -> LLMResponse:
        raise NotImplementedError
