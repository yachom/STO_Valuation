"""Repository bases (in-process)."""

from __future__ import annotations

from common.ports.repository import MarketSignalRecord, NormalizedNews, NewsRepository, SignalStore


class BaseNewsRepository(NewsRepository):
    """Default behavior shared by concrete repositories (logging/metrics)."""

    def save(self, news: NormalizedNews) -> bool:
        raise NotImplementedError

    def get(self, url_hash: str):  # type: ignore[override]
        raise NotImplementedError

    def list_since(self, since):  # type: ignore[override]
        raise NotImplementedError


class BaseSignalStore(SignalStore):
    def put(self, signal: MarketSignalRecord) -> None:
        raise NotImplementedError

    def list_by_asset(self, asset_class: str):  # type: ignore[override]
        raise NotImplementedError
