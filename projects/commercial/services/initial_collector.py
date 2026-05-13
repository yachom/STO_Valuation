"""Initial metadata collection for a commercial address.

Sequence: JUSO → VWorld → MOLIT → Building Ledger.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from projects.commercial.clients.building_ledger import BuildingLedgerClient
from projects.commercial.clients.juso import JusoClient
from projects.commercial.clients.molit import MolitClient
from projects.commercial.clients.vworld import VWorldClient


class AddressMetadata(BaseModel):
    address_input: str
    road_address: str | None = None
    jibun_address: str | None = None
    coord_x: float | None = None
    coord_y: float | None = None
    pnu: str | None = None
    building_info: dict[str, Any] = Field(default_factory=dict)
    recent_trades: list[dict[str, Any]] = Field(default_factory=list)


class InitialCollectorService:
    def __init__(
        self,
        juso: JusoClient,
        vworld: VWorldClient,
        molit: MolitClient,
        ledger: BuildingLedgerClient,
    ) -> None:
        self._juso = juso
        self._vworld = vworld
        self._molit = molit
        self._ledger = ledger

    def collect(self, address: str) -> AddressMetadata:
        raise NotImplementedError
