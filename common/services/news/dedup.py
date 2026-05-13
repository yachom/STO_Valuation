"""Pure-function semantic dedup (cosine similarity threshold)."""

from __future__ import annotations


def cosine(a: list[float], b: list[float]) -> float:
    raise NotImplementedError


def is_duplicate(emb: list[float], existing: list[list[float]], threshold: float = 0.92) -> bool:
    raise NotImplementedError
