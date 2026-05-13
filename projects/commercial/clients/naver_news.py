"""Commercial-asset Naver News wrapper bound to this asset's search_groups."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.clients.naver_news import NaverNewsClient


class CommercialNaverNewsClient:
    """Thin wrapper that pairs the common NaverNewsClient with the asset's queries."""

    def __init__(self, client: NaverNewsClient, search_groups_path: str) -> None:
        self._client = client
        self._search_groups_path = search_groups_path

    def queries(self) -> list[str]:
        """Load and flatten search_groups.yaml into query strings."""
        raise NotImplementedError

    def collect(self, *, since: datetime) -> list[dict[str, Any]]:
        raise NotImplementedError
