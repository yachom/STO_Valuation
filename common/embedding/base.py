"""Embedding client base."""

from __future__ import annotations

from common.ports.embedding import EmbeddingClient


class BaseEmbedding(EmbeddingClient):
    name: str = ""
    dim: int = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
