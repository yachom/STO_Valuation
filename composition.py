"""Dependency injection assembly — wires clients/llm/repos into asset contexts.

Each ``build_<asset>(settings) -> <Asset>Context`` function instantiates the
full graph of dependencies for one asset. PROFILE_REGISTRY maps an asset key
(CLI flag) to its builder.

Add a new asset:
  1. Create ``projects/<asset>/`` (mirror commercial layout)
  2. Define a ``build_<asset>`` function below
  3. Add entry to PROFILE_REGISTRY
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from common.config.settings import get_settings
from common.domain.asset import AssetProfile
from common.flows.exploration import ExplorationState  # noqa: F401  (re-exported)


class CommercialContext(BaseModel):
    profile: AssetProfile
    # Concrete fields (clients, services, agents, flows, MCP server) are wired
    # in build_commercial(). Skeleton keeps the type loose to avoid premature
    # import cycles.

    model_config = {"arbitrary_types_allowed": True}


class MusicContext(BaseModel):
    profile: AssetProfile

    model_config = {"arbitrary_types_allowed": True}


def build_commercial(settings: Any) -> CommercialContext:
    raise NotImplementedError


def build_music(settings: Any) -> MusicContext:
    raise NotImplementedError


PROFILE_REGISTRY: dict[str, Callable[[Any], object]] = {
    "commercial": build_commercial,
    "music": build_music,
}


def resolve(asset_key: str) -> object:
    """Build the context for an asset key using the current process settings."""
    builder = PROFILE_REGISTRY[asset_key]
    return builder(get_settings())
