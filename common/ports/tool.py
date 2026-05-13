"""Agent tool interface."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Tool(Protocol):
    """LLM-callable unit. Wraps a service with name/description/schema."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def run(self, **kwargs: Any) -> Any: ...
