"""ReportStateV2 — risk_critic + reviser 루프를 위한 필드 확장."""

from __future__ import annotations

from typing import Annotated, TypedDict


def _merge_dict(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class ReportStateV2(TypedDict, total=False):
    asset_id: str
    asset_summary: str
    asset_identifiers: dict                         # bd_mgt_sn, lat, lon, sigungu_cd, sigungu 등
    prefetched_facts: dict                          # planner가 사전 수집한 건물·입지·시장 facts
    section_outline: list[dict]
    sections: Annotated[dict[str, dict], _merge_dict]
    overview_section: dict
    validation_issues: list[dict]
    # v2 신규
    critic_output: dict
    critic_iter: int
    polished: bool
