"""Langfuse setup. Falls back to no-op handler if keys are missing."""

from __future__ import annotations

import os
from typing import Any

from . import config


def init_langfuse() -> Any | None:
    if not (config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY):
        return None
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", config.LANGFUSE_PUBLIC_KEY)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", config.LANGFUSE_SECRET_KEY)
    os.environ.setdefault("LANGFUSE_HOST", config.LANGFUSE_BASE_URL or "")

    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=config.LANGFUSE_PUBLIC_KEY,
            secret_key=config.LANGFUSE_SECRET_KEY,
            host=config.LANGFUSE_BASE_URL,
        )
        return client
    except Exception:
        return None


def langchain_callbacks() -> list[Any]:
    """Return Langfuse callback handlers for LangChain/LangGraph runs."""
    if not (config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY):
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception:
        try:
            from langfuse.callback import CallbackHandler  # legacy path

            return [
                CallbackHandler(
                    public_key=config.LANGFUSE_PUBLIC_KEY,
                    secret_key=config.LANGFUSE_SECRET_KEY,
                    host=config.LANGFUSE_BASE_URL,
                )
            ]
        except Exception:
            return []
