"""Music asset evaluation units (placeholder)."""

from __future__ import annotations

from common.domain.target import Target


class SongTarget(Target):
    isrc: str
    title: str | None = None

    @property
    def identifier(self) -> str:
        return self.isrc


class ArtistTarget(Target):
    name: str
    agency: str | None = None

    @property
    def identifier(self) -> str:
        return f"{self.name}::{self.agency or ''}"
