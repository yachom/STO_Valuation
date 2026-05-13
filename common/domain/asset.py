"""Asset-agnostic identifiers and profile."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AssetClass(str, Enum):
    COMMERCIAL = "commercial"
    MUSIC = "music"


class AssetProfile(BaseModel):
    """Metadata describing an asset family and its registry entry.

    Concrete factor dictionaries, ontology axes, and target classes live under
    ``projects/<asset>/`` and are referenced by name through this profile.
    """

    asset_class: AssetClass
    display_name: str
    description: str = ""
    config_dir: str = Field(
        ...,
        description="Filesystem path (relative to repo root) holding the asset's "
        "config files (search_groups.yaml, llm.yaml, ...).",
    )
