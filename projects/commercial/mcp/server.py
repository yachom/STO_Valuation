"""Commercial-asset MCP server — exposes this asset's tools over MCP (stub)."""

from __future__ import annotations

from common.mcp.server_base import BaseMCPServer
from projects.commercial.tools.collect_address_metadata_tool import (
    CollectAddressMetadataTool,
)
from projects.commercial.tools.search_commercial_news_tool import (
    SearchCommercialNewsTool,
)


class CommercialMCPServer(BaseMCPServer):
    name = "commercial"

    def __init__(
        self,
        collect_tool: CollectAddressMetadataTool,
        news_tool: SearchCommercialNewsTool,
    ) -> None:
        super().__init__()
        self._collect_tool = collect_tool
        self._news_tool = news_tool

    def setup(self) -> None:
        """Register all tools. Called before serve()."""
        raise NotImplementedError
