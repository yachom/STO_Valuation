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


def _make_write_node(ax):
    """축 1개의 write 노드 (Axis 클로저). 동적 축이면 intent/title/focus가 ax에 담겨
    write_axis로 전달된다. outline은 ax.key로 조회(동적 축은 보통 None)."""
    def node(state: MusicFlowState) -> dict:
        from music_copyright.graphflow.report import write_axis
        draft, evidence, violations = write_axis(
            _driver(), state["work_id"], state["work_card"], ax,
            evidence_k=state.get("evidence_k"),
            outline=(state.get("outline") or {}).get(ax.key))
        return {"sections": {ax.key: draft},
                "evidence_by_axis": {ax.key: evidence},
                "mech_audit": {ax.key: violations}}
    node.__name__ = f"write_{ax.key}"
    return node


def _make_synthesize_node(axes):
    keys = [a.key for a in axes]

    def node(state: MusicFlowState) -> dict:
        from music_copyright.graphflow.report import synthesize_overview
        sections = {k: state["sections"][k] for k in keys if k in state["sections"]}
        overview, index_map, violations = synthesize_overview(state["work_card"], sections)
        return {"overview": overview, "index_map": index_map,
                "mech_audit": {"overview": violations}}
    return node


def _make_finalize_node(axes):
    keys = [a.key for a in axes]

    def node(state: MusicFlowState) -> dict:
        from music_copyright.graphflow.report import finalize_report
        sections = {k: state["sections"][k] for k in keys if k in state["sections"]}
        report = finalize_report(
            _driver(), state["work_id"], sections, state["overview"], state["index_map"],
            state["evidence_by_axis"], state["mech_audit"], out_dir=state["out_dir"])
        return {"report": report}
    return node


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
def build_report_graph(axes=None):
    """보고서 그래프 조립. axes 미지정이면 기본 3축. 동적 축은 write 노드가 축별로
    생성되고(fan-out), synthesize/finalize가 그 축 키들을 닫아 잡는다."""
    from music_copyright.graphflow.axes import BASE_AXES
    axes = axes or BASE_AXES

    g = StateGraph(MusicFlowState)
    g.add_node("seed", seed_node)
    g.add_node("expand", expand_node)
    g.add_node("perspectives", perspectives_node)
    g.add_node("discourse_round", discourse_round_node)
    g.add_node("moderator", moderator_node)
    g.add_node("outline", outline_node)
    for ax in axes:
        g.add_node(f"write_{ax.key}", _make_write_node(ax))
    g.add_node("synthesize", _make_synthesize_node(axes))
    g.add_node("finalize", _make_finalize_node(axes))

    g.add_edge(START, "seed")
    g.add_conditional_edges("seed", route_after_seed,
                            {"expand": "expand", "perspectives": "perspectives"})
    # 담론 사이클 (기본 3축 척추에서만 관점 발견 — 동적 축은 자체 semantic 검색)
    g.add_edge("perspectives", "discourse_round")
    g.add_edge("discourse_round", "moderator")
    g.add_conditional_edges("moderator", route_after_moderator,
                            {"discourse_round": "discourse_round", "outline": "outline"})
    # 두 경로 모두 축별 fan-out (기본 3 + 동적)
    for ax in axes:
        g.add_edge("expand", f"write_{ax.key}")     # 정적 경로
        g.add_edge("outline", f"write_{ax.key}")    # 담론 경로
        g.add_edge(f"write_{ax.key}", "synthesize")
    g.add_edge("synthesize", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def run_report_pipeline(
    work_id: str,
    *,
    out_dir: str,
    evidence_k: int | None = None,
    expand_rounds: int = 0,
    axes=None,
) -> MusicFlowState:
    """LangGraph 보고서 파이프라인 실행 (그래프 사전 시드 가정).

    axes 미지정이면 기본 3축. 동적 축(사람·에이전트)은 호출부가 resolve해 넘긴다 —
    그래프가 그 축 리스트로 write 노드를 만들어 fan-out한다.
    """
    from music_copyright.graphflow.axes import BASE_AXES
    axes = axes or BASE_AXES
    app = build_report_graph(axes)
    init: MusicFlowState = {"work_id": work_id, "out_dir": out_dir,
                            "expand_rounds": expand_rounds}
    if evidence_k is not None:
        init["evidence_k"] = evidence_k
    return app.invoke(init)
