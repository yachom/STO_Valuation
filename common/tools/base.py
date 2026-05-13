"""BaseTool — service → LLM-callable wrapper.

A Tool MUST:
  - own no business logic (delegate to its underlying service)
  - declare ``input_schema`` (JSON schema) for the LLM
  - be deterministic in input/output shape
"""

from __future__ import annotations

from typing import Any


class BaseTool:
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}

    def run(self, **kwargs: Any) -> Any:
        raise NotImplementedError
