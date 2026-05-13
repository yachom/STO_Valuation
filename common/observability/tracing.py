"""Generic @traced decorator + span context manager (Langfuse-aware)."""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def traced(name: str | None = None, **attrs: Any) -> Callable[[F], F]:
    """Decorator: wrap a callable in a span. Captures args/return if cheap."""

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        return wrapper  # type: ignore[return-value]

    return decorator


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[None]:
    raise NotImplementedError
    yield  # pragma: no cover
