"""Langfuse integration — optional dependency.

If ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` are unset, all helpers
become no-ops so local/CI runs work without network access.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


def is_enabled() -> bool:
    raise NotImplementedError


@contextmanager
def trace(name: str, **attrs: Any) -> Iterator[Any]:
    """Root span context manager. Yields the active trace handle or None."""
    raise NotImplementedError
    yield None  # pragma: no cover


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    raise NotImplementedError
    yield None  # pragma: no cover


def record_generation(
    *,
    model: str,
    input_messages: list[dict[str, Any]],
    output_text: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
) -> None:
    raise NotImplementedError
