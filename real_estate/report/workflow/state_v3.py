"""ReportStateV3 — Co-STORM 기반 planner_v3 + mind_map 흡수용 state.

state_v2의 모든 필드를 유지(graph_v2 호환) + Co-STORM 식 신규 필드 추가.

Phase A는 mind_map / evidence_pool / discourse_log / prior_* 만 실제 사용.
replan_iter / reflection_memo 는 Phase B에서 graph_v3 도입 시 활성화.
"""

from __future__ import annotations

from typing import Annotated, TypedDict


def _merge_dict(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


def _append_list(left: list | None, right: list | None) -> list:
    return (left or []) + (right or [])


class ReportStateV3(TypedDict, total=False):
    # ─── v2 호환 ───
    asset_id: str
    asset_summary: str
    asset_identifiers: dict
    prefetched_facts: dict
    section_outline: list[dict]
    sections: Annotated[dict[str, dict], _merge_dict]
    overview_section: dict
    validation_issues: list[dict]
    critic_output: dict
    critic_iter: int

    # ─── v3 신규 (Phase A 사용) ───
    mind_map: dict                 # {concept_id: {label, axis, evidence_ids, focus_question, ...}}
    evidence_pool: dict            # {ev_id: {text, ref, source, axis}}
    discourse_log: Annotated[list[dict], _append_list]
    prior_human_comments: list[dict]
    prior_qa_gaps: list[dict]

    # ─── v3 신규 (Phase B 예약) ───
    reflection_memo: str
    replan_iter: int
