"""Structured logging setup (structlog).

Call ``configure_logging()`` once at app entry (cli.py, airflow scripts).
"""

from __future__ import annotations


def configure_logging(level: str = "INFO", env: str = "local") -> None:
    """Idempotent logging configuration."""
    raise NotImplementedError


def get_logger(name: str) -> object:
    """Return a structlog BoundLogger (typed as object to avoid import here)."""
    raise NotImplementedError
