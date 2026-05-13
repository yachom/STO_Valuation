"""Base MCP server. Concrete servers inherit and register their asset tools."""

from __future__ import annotations

from common.ports.tool import Tool


class BaseMCPServer:
    name: str = ""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        raise NotImplementedError

    def list_tools(self) -> list[Tool]:
        raise NotImplementedError

    def serve(self) -> None:
        """Run the MCP transport (stdio/sse)."""
        raise NotImplementedError
