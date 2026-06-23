"""LangGraph 워크플로우 (음악저작권) — STUB. 최소 파이프라인 골격.

    SEED → EXPAND(web) → [write_influence ∥ write_popularity ∥ write_sustainability]
         → SYNTHESIZE → FINALIZE

real_estate/graphflow/workflow.py의 토폴로지를 미러링하되, 노드 본문은 STUB이다
(end-to-end로 돌 필요 없음 — 그래프가 compile되고 형태가 맞는지만 본다). 3축 Writer는
fan-out 병렬, synthesize에서 fan-in. 도메인 확정 시 각 노드가 report/expand/seeder의
실제 스테이지를 호출하도록 채운다.

⚠️ 모든 노드는 STUB(# TODO(domain)). build_report_graph()는 CompiledStateGraph를
반환한다 — 합성 검증은 컴파일까지만 한다(Neo4j/LLM 불요).

사용:
    app = build_report_graph()
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from music_copyright.graphflow.profiles import REPORT_AXES

logger = logging.getLogger("music_copyright.workflow")


def _merge_dicts(a: dict | None, b: dict | None) -> dict:
    """병렬 노드들의 dict 부분 갱신 머지 (LangGraph reducer)."""
    return {**(a or {}), **(b or {})}


class MusicFlowState(TypedDict, total=False):
    """워크플로우 상태 — 포인터/진행 정보만(작업 메모리는 Neo4j). STUB 범위 최소.

    real_estate.GraphFlowState를 미러링한 축소판. 3축 Writer가 병렬이라 dict 필드는
    머지 리듀서를 단다.
    """
    # ── 입력 ──
    work_id: str
    out_dir: str
    # ── 제어 ──
    expand_rounds: int
    # ── 단계 산출 ──
    work_card: str
    expand_summary: dict
    sections: Annotated[dict, _merge_dicts]          # axis → SectionDraft (STUB)
    overview: Any
    report: dict


# ───────────────────────────────────────────────────────────
# 노드 (전부 STUB — # TODO(domain))
# ───────────────────────────────────────────────────────────
def seed_node(state: MusicFlowState) -> dict:
    """웹검색 우선 시드 (STUB). 도메인 확정 시 seeder.collect_scope→seed 호출."""
    # TODO(domain): driver 확보 + seed(collect_scope(state["work_id"]), driver)
    return {"work_card": f"(STUB work_card for {state.get('work_id')})"}


def expand_node(state: MusicFlowState) -> dict:
    """웹검색 확장 (STUB). 도메인 확정 시 run_expand(ddgs/naver/tavily) 호출."""
    # TODO(domain): navigator→harvester 라운드 루프(infra.tool 웹검색).
    rounds = state.get("expand_rounds", 0)
    return {"expand_summary": {"rounds_run": 0 if rounds <= 0 else rounds, "stub": True}}


def _make_write_node(axis: str):
    def node(state: MusicFlowState) -> dict:
        # TODO(domain): traversal.walk + StaticPolicy(axis) → evidence → writer 집필.
        return {"sections": {axis: {"axis": axis, "stub": True}}}
    node.__name__ = f"write_{axis}"
    return node


def synthesize_node(state: MusicFlowState) -> dict:
    """3축 종합 등급 판정 (STUB)."""
    # TODO(domain): synthesizer가 axis_scores(A+~D) + overall_grade 산출.
    return {"overview": {"stub": True, "axes": list(state.get("sections", {}))}}


def finalize_node(state: MusicFlowState) -> dict:
    """보고서 직렬화 (STUB)."""
    # TODO(domain): finalize_report(driver, ...) → 구조화 JSON + 사이드카 트레이스.
    return {"report": {"work_id": state.get("work_id"), "stub": True}}


# ───────────────────────────────────────────────────────────
# 그래프 조립 — 토폴로지가 곧 흐름의 문서 (real_estate 미러)
# ───────────────────────────────────────────────────────────
def build_report_graph():
    """LangGraph 조립 (STUB 노드).

        START → seed → expand ─┬─ write_influence ──┐
                               ├─ write_popularity ─┤→ synthesize → finalize → END
                               └─ write_sustainability ┘
    """
    g = StateGraph(MusicFlowState)
    g.add_node("seed", seed_node)
    g.add_node("expand", expand_node)
    for axis in REPORT_AXES:
        g.add_node(f"write_{axis}", _make_write_node(axis))
    g.add_node("synthesize", synthesize_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "seed")
    g.add_edge("seed", "expand")
    for axis in REPORT_AXES:                 # fan-out: 3축 병렬
        g.add_edge("expand", f"write_{axis}")
        g.add_edge(f"write_{axis}", "synthesize")   # fan-in (전 축 완료 대기)
    g.add_edge("synthesize", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
