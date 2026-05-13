"""Music-copyright asset profile (placeholder)."""

from __future__ import annotations

from common.domain.asset import AssetClass, AssetProfile

MUSIC_PROFILE = AssetProfile(
    asset_class=AssetClass.MUSIC,
    display_name="Music Copyright",
    description="K-pop/가요 저작권 신탁 자산 — ISRC 단위 평가 (placeholder)",
    config_dir="projects/music/config",
)
