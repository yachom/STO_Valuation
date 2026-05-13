"""MOLIT (국토교통부) 실거래가 API client."""

from __future__ import annotations

from typing import Any

from common.clients.base import BaseClient


class MolitClient(BaseClient):
    api_name = "molit"
    base_url = "https://apis.data.go.kr/1613000"

    def commercial_trades(self, *, lawd_cd: str, deal_ymd: str) -> dict[str, Any]:
        """실거래가 — 상업업무용 부동산."""
        raise NotImplementedError
