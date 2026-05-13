"""External MCP server connector — exposes remote tools as local Tool instances."""

from __future__ import annotations

from typing import Any

from common.ports.tool import Tool


class MCPClientAdapter:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def connect(self) -> None:
        raise NotImplementedError

    def list_tools(self) -> list[Tool]:
        raise NotImplementedError

    def call(self, name: str, **kwargs: Any) -> Any:
        raise NotImplementedError
