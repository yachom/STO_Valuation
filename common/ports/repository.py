"""Storage ports for batch-collected artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class NormalizedNews(BaseModel):
    url: str
    url_hash: str
    title: str
    body: str
    published_at: datetime | None = None
    source: str | None = None
    raw_meta: dict[str, Any] = Field(default_factory=dict)


class MarketSignalRecord(BaseModel):
    signal_id: str
    asset_class: str
    payload: dict[str, Any]
    created_at: datetime


@runtime_checkable
class NewsRepository(Protocol):
    def save(self, news: NormalizedNews) -> bool:
        """Return False if duplicate (url_hash already present)."""

    def get(self, url_hash: str) -> NormalizedNews | None: ...

    def list_since(self, since: datetime) -> list[NormalizedNews]: ...


@runtime_checkable
class SignalStore(Protocol):
    def put(self, signal: MarketSignalRecord) -> None: ...

    def list_by_asset(self, asset_class: str) -> list[MarketSignalRecord]: ...
