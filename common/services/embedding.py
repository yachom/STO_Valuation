"""Thin service wrapper around EmbeddingClient (port-level convenience)."""

from __future__ import annotations

from common.ports.embedding import EmbeddingClient


class EmbeddingService:
    def __init__(self, client: EmbeddingClient) -> None:
        self._client = client

    def embed_one(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
