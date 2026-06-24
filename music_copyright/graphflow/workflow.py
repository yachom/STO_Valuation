"""LangGraph 워크플로우 (음악저작권) — 보고서 파이프라인 토폴로지. real_estate 미러.

정적 경로 (curation.mode=static|moderated):
    START → seed → expand → [write_influence ∥ write_popularity ∥ write_sustainability]
          → synthesize → finalize → END

담론 경로 (curation.mode=discourse, expand_rounds>0):
    START → seed → perspectives → discourse_round ⟲ moderator → outline
          → [write_axis ×3] → synthesize → finalize → END

토폴로지가 곧 흐름의 문서. 각 노드는 실제 단계 함수를 호출(드라이버는 음악 전용 싱글톤).
expand는 시드 선행 가정. LLM은 전부 GPT 티어.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from music_copyright.graphflow.profiles import REPORT_AXES

logger = logging.getLogger("music_copyright.workflow")


def _merge_dicts(a: dict | None, b: dict | None) -> dict:
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
    # ── 담론(discourse) ──
    discourse_threads: list
    discourse_round: int
    moderator_done: bool
    outline: dict
    # ── 집필 fan-out (머지 리듀서) ──
    sections: Annotated[dict, _merge_dicts]
    evidence_by_axis: Annotated[dict, _merge_dicts]
    mech_audit: Annotated[dict, _merge_dicts]
    overview: Any
    index_map: list
    report: dict


def _driver():
    from music_copyright.graphflow.graph.driver import get_music_driver
    return get_music_driver()


def _curation_mode() -> str:
    from music_copyright.graphflow.config import get_config
    return get_config().get("curation", {}).get("mode", "static")


# ───────────────────────────────────────────────────────────
# 노드
# ───────────────────────────────────────────────────────────
def seed_node(state: MusicFlowState) -> dict:
    """Work 카드 확보 (그래프 사전 시드 가정)."""
    from music_copyright.graphflow.graph.queries import work_card_text
    return {"work_card": work_card_text(_driver(), state["work_id"])}


def expand_node(state: MusicFlowState) -> dict:
    """웹검색 확장 (static/moderated). expand_rounds>0일 때 run_expand."""
    rounds = state.get("expand_rounds", 0)
    if rounds <= 0:
        return {"expand_summary": {"rounds_run": 0, "skipped": True}}
    from infra.agent.tracing import make_run_id
    from music_copyright.graphflow.expand import run_expand
    collected_by = state.get("collected_by") or make_run_id("exp")
    summary = run_expand(_driver(), work_id=state["work_id"],
                         collected_by=collected_by, rounds=rounds)
    if _curation_mode() == "moderated":
        from music_copyright.graphflow.moderator import run_moderated_expand
        summary["moderator"] = run_moderated_expand(
            _driver(), work_id=state["work_id"], collected_by=collected_by)
    return {"expand_summary": summary, "collected_by": collected_by}


# ── 담론 노드 ──
def perspectives_node(state: MusicFlowState) -> dict:
    from music_copyright.graphflow.discourse import perspectives_stage
    threads = perspectives_stage(_driver(), state["work_id"])
    return {"discourse_threads": threads, "discourse_round": 0, "moderator_done": False}


def discourse_round_node(state: MusicFlowState) -> dict:
    from infra.agent.tracing import make_run_id
    from music_copyright.graphflow.discourse import discourse_round_stage
    collected_by = state.get("collected_by") or make_run_id("exp")
    threads = discourse_round_stage(_driver(), state["work_id"],
                                    state["discourse_threads"], collected_by)
    return {"discourse_threads": threads,
            "discourse_round": state.get("discourse_round", 0) + 1,
            "collected_by": collected_by}


def moderator_node(state: MusicFlowState) -> dict:
    from infra.agent.tracing import make_run_id
    from music_copyright.graphflow.discourse import discourse_max_rounds
    from music_copyright.graphflow.moderator import moderator_round_stage
    collected_by = state.get("collected_by") or make_run_id("exp")
    res = moderator_round_stage(_driver(), state["work_id"],
                                state.get("discourse_round", 0),
                                discourse_max_rounds(), collected_by)
    return {"moderator_done": res["done"]}


def outline_node(state: MusicFlowState) -> dict:
    from music_copyright.graphflow.discourse import outline_stage
    outline = outline_stage(_driver(), state["work_id"], state["discourse_threads"])
    return {"outline": outline,
            "expand_summary": {"mode": "discourse",
                               "rounds_run": state.get("discourse_round", 0)}}


def _make_write_node(axis: str):
    def node(state: MusicFlowState) -> dict:
        from music_copyright.graphflow.report import write_axis
        draft, evidence, violations = write_axis(
            _driver(), state["work_id"], state["work_card"], axis,
            evidence_k=state.get("evidence_k"),
            outline=(state.get("outline") or {}).get(axis))
        return {"sections": {axis: draft},
                "evidence_by_axis": {axis: evidence},
                "mech_audit": {axis: violations}}
    node.__name__ = f"write_{axis}"
    return node


def synthesize_node(state: MusicFlowState) -> dict:
    from music_copyright.graphflow.report import synthesize_overview
    sections = {ax: state["sections"][ax] for ax in REPORT_AXES if ax in state["sections"]}
    overview, index_map, violations = synthesize_overview(state["work_card"], sections)
    return {"overview": overview, "index_map": index_map,
            "mech_audit": {"overview": violations}}


def finalize_node(state: MusicFlowState) -> dict:
    from music_copyright.graphflow.report import finalize_report
    sections = {ax: state["sections"][ax] for ax in REPORT_AXES if ax in state["sections"]}
    report = finalize_report(
        _driver(), state["work_id"], sections, state["overview"], state["index_map"],
        state["evidence_by_axis"], state["mech_audit"], out_dir=state["out_dir"])
    return {"report": report}


# ───────────────────────────────────────────────────────────
# 라우팅
# ───────────────────────────────────────────────────────────
def route_after_seed(state: MusicFlowState) -> str:
    """discourse 모드 + expand_rounds>0 → 담론 사이클; 아니면 정적 expand."""
    if _curation_mode() == "discourse" and state.get("expand_rounds", 0) > 0:
        return "perspectives"
    return "expand"


def route_after_moderator(state: MusicFlowState) -> str:
    """담론 사이클 종료: moderator_done 또는 라운드 상한 → outline."""
    from music_copyright.graphflow.discourse import discourse_max_rounds
    if state.get("moderator_done") or state.get("discourse_round", 0) >= discourse_max_rounds():
        return "outline"
    return "discourse_round"


# ───────────────────────────────────────────────────────────
# 그래프 조립
# ───────────────────────────────────────────────────────────
def build_report_graph():
    g = StateGraph(MusicFlowState)
    g.add_node("seed", seed_node)
    g.add_node("expand", expand_node)
    g.add_node("perspectives", perspectives_node)
    g.add_node("discourse_round", discourse_round_node)
    g.add_node("moderator", moderator_node)
    g.add_node("outline", outline_node)
    for axis in REPORT_AXES:
        g.add_node(f"write_{axis}", _make_write_node(axis))
    g.add_node("synthesize", synthesize_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "seed")
    g.add_conditional_edges("seed", route_after_seed,
                            {"expand": "expand", "perspectives": "perspectives"})
    # 담론 사이클
    g.add_edge("perspectives", "discourse_round")
    g.add_edge("discourse_round", "moderator")
    g.add_conditional_edges("moderator", route_after_moderator,
                            {"discourse_round": "discourse_round", "outline": "outline"})
    # 두 경로 모두 3축 fan-out
    for axis in REPORT_AXES:
        g.add_edge("expand", f"write_{axis}")     # 정적 경로
        g.add_edge("outline", f"write_{axis}")    # 담론 경로
        g.add_edge(f"write_{axis}", "synthesize")
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
