"""Pure utility functions (no I/O, no state)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """Strip fragment, lowercase scheme/host. Leave path/query intact."""
    raise NotImplementedError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["sha256_hex", "normalize_url", "utc_now"]
