"""LangGraph 그래프가 다루는 ReportState — 모든 노드가 partial update."""

from __future__ import annotations

from typing import Annotated, TypedDict


def _merge_dict(left: dict | None, right: dict | None) -> dict:
    """sections 같은 dict[section_id, section_dict]는 노드별 partial로 들어오므로
    union으로 머지한다 (오른쪽 우선)."""
    return {**(left or {}), **(right or {})}


class ReportState(TypedDict, total=False):
    asset_id: str
    asset_summary: str
    asset_identifiers: dict                         # bd_mgt_sn, lat, lon, sigungu_cd, sigungu 등
    prefetched_facts: dict                          # planner가 사전 수집한 건물·입지·시장 facts
    section_outline: list[dict]                     # planner 결과
    sections: Annotated[dict[str, dict], _merge_dict]  # axis별 analyst 결과 (병렬 머지)
    overview_section: dict                          # synthesizer 결과
    validation_issues: list[dict]                   # validator 결과
