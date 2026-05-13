"""Commercial-specific pure utilities (PNU parsing, address normalization, ...)."""

from __future__ import annotations


def is_valid_pnu(pnu: str) -> bool:
    """19-digit numeric PNU validator."""
    raise NotImplementedError


def normalize_korean_address(address: str) -> str:
    raise NotImplementedError
