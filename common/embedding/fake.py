"""Deterministic fake embedding (hash-based) for tests/skeleton."""

from __future__ import annotations

from common.embedding.base import BaseEmbedding


class FakeEmbedding(BaseEmbedding):
    name = "fake"
    dim = 64

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
