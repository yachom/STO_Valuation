"""Generic web search client (placeholder — provider TBD: Tavily/Serper/...)."""

from __future__ import annotations

from typing import Any

from common.clients.base import BaseClient


class WebSearchClient(BaseClient):
    api_name = "web_search"

    def search(self, query: str, *, max_results: int = 10) -> dict[str, Any]:
        raise NotImplementedError
