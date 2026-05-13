"""Base HTTP client.

Concrete clients inherit and define endpoint methods. The base owns:
  - httpx client lifecycle
  - retry / rate-limit
  - structured logging
  - per-API quota counter (observability.quota)
  - latency / status spans (observability.tracing)

Concrete clients MUST NOT import domain or services.
"""

from __future__ import annotations

from typing import Any

import httpx


class BaseClient:
    """Asset-agnostic HTTP client base."""

    api_name: str = ""  # subclass override — used for quota/log labels
    base_url: str = ""
    default_timeout: float = 10.0
    daily_quota: int | None = None  # if set, BaseClient enforces per-day limit

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = api_key
        self._client = client
        self._timeout = timeout or self.default_timeout

    def _http(self) -> httpx.Client:
        raise NotImplementedError

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute one HTTP call. Handles retry/quota/log. Returns parsed JSON."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
