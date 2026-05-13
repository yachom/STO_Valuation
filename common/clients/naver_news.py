"""Naver News search API client (asset-agnostic).

Asset-specific search_groups are applied by the caller, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.clients.base import BaseClient


class NaverNewsClient(BaseClient):
    api_name = "naver_news"
    base_url = "https://openapi.naver.com"

    def search(
        self,
        query: str,
        *,
        since: datetime | None = None,
        display: int = 100,
        start: int = 1,
        sort: str = "date",
    ) -> dict[str, Any]:
        """One call to /v1/search/news.json. Returns raw JSON dict."""
        raise NotImplementedError
