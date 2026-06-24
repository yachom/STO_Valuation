"""LangGraph 워크플로우 (음악저작권) — 보고서 파이프라인 토폴로지. real_estate 미러.

    SEED → EXPAND(web, TODO) → [write_influence ∥ write_popularity ∥ write_sustainability]
         → SYNTHESIZE → FINALIZE

토폴로지가 곧 흐름의 문서다. 3축 Writer는 fan-out 병렬, synthesize에서 fan-in.
각 노드는 report.py의 실제 단계 함수를 호출한다(드라이버는 get_neo4j_driver 싱글톤).
그래프는 시드 선행 가정(run_report_pipeline가 시드까지 책임지거나 사전 시드).

⚠️ expand_node는 v1에선 패스스루(웹검색 EXPAND는 후속). LLM은 전부 GPT 티어.

사용:
    app = build_report_graph()
    state = run_report_pipeline(work_id="T-300.516.442-7", out_dir="reports/")
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
    # ── 입력 ──
    work_id: str
    out_dir: str
    evidence_k: int
    expand_rounds: int
    collected_by: str
    # ── 단계 산출 ──
    work_card: str
    expand_summary: dict
    sections: Annotated[dict, _merge_dicts]            # axis → SectionDraft
    evidence_by_axis: Annotated[dict, _merge_dicts]    # axis → list[Evidence]
    mech_audit: Annotated[dict, _merge_dicts]          # axis → list[violation]
    overview: Any
    index_map: list
    report: dict


# ───────────────────────────────────────────────────────────
# 노드 — report.py 단계 함수 호출 (driver는 싱글톤)
# ───────────────────────────────────────────────────────────
def _driver():
    from music_copyright.graphflow.graph.driver import get_music_driver
    return get_music_driver()


def seed_node(state: MusicFlowState) -> dict:
    """Work 카드 확보 (그래프 사전 시드 가정). 없으면 ValueError."""
    from music_copyright.graphflow.graph.queries import work_card_text
    return {"work_card": work_card_text(_driver(), state["work_id"])}


def expand_node(state: MusicFlowState) -> dict:
    """웹검색 확장 — navigator→harvester→curator 라운드 (expand_rounds>0일 때)."""
    rounds = state.get("expand_rounds", 0)
    if rounds <= 0:
        return {"expand_summary": {"rounds_run": 0, "skipped": True}}
    from infra.agent.tracing import make_run_id
    from music_copyright.graphflow.expand import run_expand
    collected_by = state.get("collected_by") or make_run_id("exp")
    summary = run_expand(_driver(), work_id=state["work_id"],
                         collected_by=collected_by, rounds=rounds)
    return {"expand_summary": summary, "collected_by": collected_by}


def _make_write_node(axis: str):
    def node(state: MusicFlowState) -> dict:
        from music_copyright.graphflow.report import write_axis
        draft, evidence, violations = write_axis(
            _driver(), state["work_id"], state["work_card"], axis,
            evidence_k=state.get("evidence_k"))
        return {"sections": {axis: draft},
                "evidence_by_axis": {axis: evidence},
                "mech_audit": {axis: violations}}
    node.__name__ = f"write_{axis}"
    return node


def synthesize_node(state: MusicFlowState) -> dict:
    """3축 종합 등급 판정."""
    from music_copyright.graphflow.report import synthesize_overview
    # REPORT_AXES 순서로 정렬해 [C#] 인덱스를 결정적으로 유지
    sections = {ax: state["sections"][ax] for ax in REPORT_AXES if ax in state["sections"]}
    overview, index_map, violations = synthesize_overview(state["work_card"], sections)
    return {"overview": overview, "index_map": index_map,
            "mech_audit": {"overview": violations}}


def finalize_node(state: MusicFlowState) -> dict:
    """보고서 조립·저장·서브그래프 적재."""
    from music_copyright.graphflow.report import finalize_report
    sections = {ax: state["sections"][ax] for ax in REPORT_AXES if ax in state["sections"]}
    report = finalize_report(
        _driver(), state["work_id"], sections, state["overview"], state["index_map"],
        state["evidence_by_axis"], state["mech_audit"], out_dir=state["out_dir"])
    return {"report": report}


# ───────────────────────────────────────────────────────────
# 그래프 조립 — 토폴로지가 곧 흐름의 문서 (real_estate 미러)
# ───────────────────────────────────────────────────────────
def build_report_graph():
    """LangGraph 조립.

        START → seed → expand ─┬─ write_influence ──────┐
                               ├─ write_popularity ─────┤→ synthesize → finalize → END
                               └─ write_sustainability ─┘
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


def run_report_pipeline(
    work_id: str,
    *,
    out_dir: str,
    evidence_k: int | None = None,
    expand_rounds: int = 0,
) -> MusicFlowState:
    """LangGraph 보고서 파이프라인 실행 (그래프 사전 시드 가정)."""
    app = build_report_graph()
    init: MusicFlowState = {"work_id": work_id, "out_dir": out_dir,
                            "expand_rounds": expand_rounds}
    if evidence_k is not None:
        init["evidence_k"] = evidence_k
    return app.invoke(init)
