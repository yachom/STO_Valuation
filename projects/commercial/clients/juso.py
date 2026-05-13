"""JUSO (도로명주소) API client."""

from __future__ import annotations

from typing import Any

from common.clients.base import BaseClient


class JusoClient(BaseClient):
    api_name = "juso"
    base_url = "https://business.juso.go.kr"

    def search(self, keyword: str, *, current_page: int = 1, count_per_page: int = 10) -> dict[str, Any]:
        raise NotImplementedError
