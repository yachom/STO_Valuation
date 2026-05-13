"""LLM provider interface."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class Capability(str, Enum):
    TOOL_CALLING = "tool_calling"
    JSON_MODE = "json_mode"
    STREAMING = "streaming"
    VISION = "vision"


class LLMMessage(BaseModel):
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None


class LLMResponse(BaseModel):
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    raw: dict[str, Any] | None = None


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic LLM surface (minimum common denominator)."""

    name: str
    capabilities: set[Capability]

    def generate(
        self,
        messages: list[LLMMessage],
        *,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...
