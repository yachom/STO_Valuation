"""Pure-function news normalization (raw dict → NormalizedNews)."""

from __future__ import annotations

from typing import Any

from common.ports.repository import NormalizedNews


def normalize_html(html: str) -> str:
    """Strip tags, collapse whitespace, decode entities."""
    raise NotImplementedError


def normalize_item(raw: dict[str, Any], *, source: str) -> NormalizedNews:
    """Map one provider record (e.g. Naver News item) to NormalizedNews."""
    raise NotImplementedError
