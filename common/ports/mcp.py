"""MCP server/client contracts."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from common.ports.tool import Tool


@runtime_checkable
class MCPServer(Protocol):
    name: str

    def register(self, tool: Tool) -> None: ...

    def serve(self) -> None: ...


@runtime_checkable
class MCPClient(Protocol):
    """Connect to an external MCP server and expose its tools as local Tools."""

    def connect(self, endpoint: str) -> None: ...

    def list_tools(self) -> list[Tool]: ...

    def call(self, name: str, **kwargs: Any) -> Any: ...
