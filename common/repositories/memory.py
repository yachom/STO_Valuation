"""In-memory implementations — for tests and skeleton runs only."""

from __future__ import annotations

from datetime import datetime

from common.ports.repository import MarketSignalRecord, NormalizedNews
from common.repositories.base import BaseNewsRepository, BaseSignalStore


class MemoryNewsRepository(BaseNewsRepository):
    def __init__(self) -> None:
        self._by_hash: dict[str, NormalizedNews] = {}

    def save(self, news: NormalizedNews) -> bool:
        raise NotImplementedError

    def get(self, url_hash: str) -> NormalizedNews | None:
        raise NotImplementedError

    def list_since(self, since: datetime) -> list[NormalizedNews]:
        raise NotImplementedError


class MemorySignalStore(BaseSignalStore):
    def __init__(self) -> None:
        self._records: list[MarketSignalRecord] = []

    def put(self, signal: MarketSignalRecord) -> None:
        raise NotImplementedError

    def list_by_asset(self, asset_class: str) -> list[MarketSignalRecord]:
        raise NotImplementedError
