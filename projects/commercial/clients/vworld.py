"""VWorld API client — coordinates, cadastral, PNU lookup."""

from __future__ import annotations

from typing import Any

from common.clients.base import BaseClient


class VWorldClient(BaseClient):
    api_name = "vworld"
    base_url = "https://api.vworld.kr"

    def address_to_coord(self, address: str) -> dict[str, Any]:
        raise NotImplementedError

    def cadastral_by_coord(self, x: float, y: float) -> dict[str, Any]:
        """Return parcel info including PNU."""
        raise NotImplementedError
