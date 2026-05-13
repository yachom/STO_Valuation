"""Process-wide settings loaded from environment.

The actual ``Settings`` class subclasses ``pydantic_settings.BaseSettings`` —
that dependency is loaded lazily inside ``get_settings()`` so the skeleton can
be imported in environments where dev deps aren't installed yet.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def get_settings() -> Any:
    """Build and return the Settings instance.

    Imports ``pydantic_settings`` lazily so that merely importing this module
    doesn't require the dev dependency to be present.
    """
    from pydantic import Field  # noqa: WPS433  (local import is intentional)
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class Settings(BaseSettings):  # type: ignore[misc]
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        env: str = "local"
        log_level: str = "INFO"

        # LLM
        anthropic_api_key: str | None = None
        openai_api_key: str | None = None
        llm_planner: str | None = None
        llm_retriever: str | None = None
        llm_critic: str | None = None

        # Observability
        langfuse_public_key: str | None = None
        langfuse_secret_key: str | None = None
        langfuse_host: str = "https://cloud.langfuse.com"

        # External APIs (commercial)
        naver_client_id: str | None = None
        naver_client_secret: str | None = None
        juso_api_key: str | None = None
        vworld_api_key: str | None = None
        molit_api_key: str | None = None
        building_ledger_api_key: str | None = None

        # Pipeline thresholds
        dedup_similarity_threshold: float = Field(default=0.92, ge=0.0, le=1.0)

    return Settings()  # type: ignore[call-arg]


# Re-exported type for annotations (lazy — string form to avoid the import here)
Settings = "common.config.settings.Settings"  # type: ignore[assignment]
