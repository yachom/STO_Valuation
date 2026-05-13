"""Abstract evaluation unit (a single thing being valued)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class Target(BaseModel, ABC):
    """Asset-agnostic base for evaluation units.

    Each concrete subclass (BuildingTarget, SongTarget, ...) lives under its
    ``projects/<asset>/domain/targets.py`` and must define a stable identifier.
    """

    @property
    @abstractmethod
    def identifier(self) -> str:
        """Canonical, globally unique id for this target (PNU, ISRC, ...)."""

    model_config = {"frozen": True}
