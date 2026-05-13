"""LLM tool wrapping InitialCollectorService."""

from __future__ import annotations

from typing import Any

from common.tools.base import BaseTool
from projects.commercial.services.initial_collector import InitialCollectorService


class CollectAddressMetadataTool(BaseTool):
    name = "collect_address_metadata"
    description = (
        "Resolve a Korean street/jibun address into building metadata "
        "(road/jibun, coordinates, PNU, building ledger title info, recent trades)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Korean address string"},
        },
        "required": ["address"],
    }

    def __init__(self, service: InitialCollectorService) -> None:
        self._service = service

    def run(self, **kwargs: Any) -> Any:
        raise NotImplementedError
