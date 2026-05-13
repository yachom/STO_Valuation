"""LLM tool wrapping the commercial Naver-news client."""

from __future__ import annotations

from typing import Any

from common.tools.base import BaseTool
from projects.commercial.clients.naver_news import CommercialNaverNewsClient


class SearchCommercialNewsTool(BaseTool):
    name = "search_commercial_news"
    description = (
        "Search recent news relevant to commercial real estate (rent, vacancy, "
        "tenant industry, urban planning, interest rates)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "since_days": {"type": "integer", "default": 30},
        },
        "required": ["query"],
    }

    def __init__(self, client: CommercialNaverNewsClient) -> None:
        self._client = client

    def run(self, **kwargs: Any) -> Any:
        raise NotImplementedError
