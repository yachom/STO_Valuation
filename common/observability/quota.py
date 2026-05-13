"""Per-day external-API quota counter.

BaseClient calls ``increment(api, n=1)`` after each successful request.
``check(api, limit_per_day)`` raises QuotaExceededError when exceeded.
"""

from __future__ import annotations

from datetime import date


def increment(api: str, n: int = 1, day: date | None = None) -> int:
    """Return the new count for (api, day). Today by default."""
    raise NotImplementedError


def get(api: str, day: date | None = None) -> int:
    raise NotImplementedError


def check(api: str, limit_per_day: int, day: date | None = None) -> None:
    """Raise QuotaExceededError if today's count >= limit."""
    raise NotImplementedError
