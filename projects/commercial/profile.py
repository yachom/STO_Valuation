"""Commercial real-estate asset profile."""

from __future__ import annotations

from common.domain.asset import AssetClass, AssetProfile

COMMERCIAL_PROFILE = AssetProfile(
    asset_class=AssetClass.COMMERCIAL,
    display_name="Commercial Real Estate",
    description="Office buildings / 근린생활시설 — PNU/사업자번호 단위 평가",
    config_dir="projects/commercial/config",
)
