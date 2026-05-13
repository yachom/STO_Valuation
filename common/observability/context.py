"""Per-request context (correlation_id / scope_id) via ContextVar.

``scope_id`` partitions on-demand analysis artifacts (graph nodes, signals,
cost accumulator) so concurrent requests don't bleed into each other.
"""

from __future__ import annotations

from contextvars import ContextVar

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
scope_id: ContextVar[str | None] = ContextVar("scope_id", default=None)


def set_scope(scope: str, correlation: str | None = None) -> None:
    raise NotImplementedError


def get_scope() -> str | None:
    raise NotImplementedError
