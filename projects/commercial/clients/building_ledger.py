"""건축물대장 API client (건축hub 또는 정부24 시리즈)."""

from __future__ import annotations

from typing import Any

from common.clients.base import BaseClient


class BuildingLedgerClient(BaseClient):
    api_name = "building_ledger"
    base_url = "https://apis.data.go.kr/1613000/BldRgstHubService"

    def title_info(self, *, pnu: str) -> dict[str, Any]:
        """건축물대장 표제부 (구조·용도·층수 등)."""
        raise NotImplementedError
