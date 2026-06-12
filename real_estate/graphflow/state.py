"""GraphFlowState — LangGraph 워크플로우 상태 (설계 §7).

원칙: 작업 메모리(증거·수집물·주장)는 전부 Neo4j 그래프 — state는 포인터와
워크플로우 진행 정보만 담는다. 예외는 집필 중간 산출(SectionDraft/Evidence)
— 보고서로 조립되기 전의 run 한정 페이로드라 state로 흐른다.

3축 Writer가 병렬 fan-out이므로 dict 필드는 머지 리듀서가 필요하다
(각 노드가 {axis: ...} 한 조각씩 반환 → 합쳐짐).
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_dicts(a: dict | None, b: dict | None) -> dict:
    """병렬 노드들의 dict 부분 갱신 머지 (LangGraph reducer)."""
    return {**(a or {}), **(b or {})}


class GraphFlowState(TypedDict, total=False):
    # ── 입력 (포인터) ──
    asset_id: str
    location: Any              # LocationInfo — seed 노드용 (이미 시드됐으면 생략)
    db_path: str
    out_dir: str

    # ── 제어 ──
    expand_rounds: int         # 0이면 EXPAND 생략 (그래프 기존 상태 사용)
    expand_budget: int         # 총 검색 쿼리 예산
    evidence_k: int            # 섹션당 evidence 수
    collected_by: str          # 이번 run id (수집물 provenance)

    # ── 단계 산출 ──
    asset_card: str
    expand_summary: dict
    sections: Annotated[dict, merge_dicts]          # axis → SectionDraft
    evidence_by_axis: Annotated[dict, merge_dicts]  # axis → list[Evidence]
    mech_audit: Annotated[dict, merge_dicts]        # axis → 잔여 위반 목록
    overview: Any                                   # OverviewDraft
    index_map: list
    report: dict                                    # 최종 구조화 JSON
