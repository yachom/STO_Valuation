"""Anthropic Claude adapter."""

from __future__ import annotations

from typing import Any

from common.llm.base import BaseLLM
from common.ports.llm import Capability, LLMMessage, LLMResponse


class AnthropicLLM(BaseLLM):
    name = "anthropic"
    capabilities = {Capability.TOOL_CALLING, Capability.JSON_MODE, Capability.STREAMING}

    def _call_provider(
        self,
        messages: list[LLMMessage],
        *,
        json_schema: dict[str, Any] | None,
        temperature: float,
        max_tokens: int | None,
    ) -> LLMResponse:
        raise NotImplementedError
