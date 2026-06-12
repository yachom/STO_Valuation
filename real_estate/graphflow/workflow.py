"""LangGraph 워크플로우 — 보고서 파이프라인의 표준 진입점 (설계 §7).

    SEED → EXPAND → [write_tenant ∥ write_esg ∥ write_commercial] → SYNTH → FINALIZE

- 3축 Writer는 LangGraph fan-out으로 병렬 실행 (LLM I/O 동시) — 순차 대비 ~3배.
- EXPAND는 노드 1개가 run_expand(내부 라운드 루프 + novelty 종료)를 호출 —
  라운드를 LangGraph 사이클로 펴는 것은 중복이라 phase 단위로 둔다.
- AUDIT 기계층은 write/synth 노드 안(재작성 1회), LLM층은 finalize 안 —
  PR-F에서 critic 재진입·발행자 합의 루프(interrupt/checkpoint)가 이 그래프에
  조건 엣지로 붙는다.
- Langfuse: get_langfuse_handler를 invoke config callbacks로 전달 — 그래프
  단위 trace가 LLM/agent span들의 상위로 잡힌다.

사용:
    app = build_report_graph()
    state = run_report_pipeline(driver, asset_id=..., out_dir=..., expand_rounds=0)
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from neo4j import Driver

from real_estate.graphflow.profiles import REPORT_AXES
from real_estate.graphflow.state import GraphFlowState
from real_estate.graphflow.tracing import make_run_id

logger = logging.getLogger("graphflow.workflow")

_driver: Driver | None = None        # 노드 공유 (Driver는 스레드 안전)


def _get_driver() -> Driver:
    global _driver
    if _driver is None:
        from real_estate.graphflow.graph.driver import get_neo4j_driver
        _driver = get_neo4j_driver()
    return _driver


# ───────────────────────────────────────────────────────────
# 노드
# ───────────────────────────────────────────────────────────
def seed_node(state: GraphFlowState) -> dict:
    """location이 있으면 스코프 시드 (idempotent). asset_card 확보."""
    from real_estate.graphflow.report import _asset_card
    from real_estate.graphflow.seeder import collect_scope, seed

    driver = _get_driver()
    if state.get("location") is not None:
        seed(collect_scope(state["location"], state["db_path"]), driver)
    return {"asset_card": _asset_card(driver, state["asset_id"])}


def expand_node(state: GraphFlowState) -> dict:
    """EXPAND (rounds=0이면 생략). 내부에서 라운드 루프 + novelty 종료."""
    rounds = state.get("expand_rounds", 0)
    if rounds <= 0:
        return {"expand_summary": {"rounds_run": 0, "skipped": True}}
    from real_estate.graphflow.expand import run_expand
    summary = run_expand(
        _get_driver(), asset_id=state["asset_id"],
        collected_by=state.get("collected_by", make_run_id("exp")),
        rounds=rounds,
        total_query_budget=state.get("expand_budget", 40))
    return {"expand_summary": summary}


def _make_write_node(axis: str):
    def node(state: GraphFlowState) -> dict:
        from real_estate.graphflow.report import write_axis
        draft, evidence, violations = write_axis(
            _get_driver(), state["asset_id"], state["asset_card"], axis,
            evidence_k=state.get("evidence_k", 12))
        return {"sections": {axis: draft},
                "evidence_by_axis": {axis: evidence},
                "mech_audit": {axis: violations}}
    node.__name__ = f"write_{axis}"
    return node


def synthesize_node(state: GraphFlowState) -> dict:
    from real_estate.graphflow.report import synthesize_overview
    overview, index_map, violations = synthesize_overview(
        state["asset_card"], state["sections"])
    return {"overview": overview, "index_map": index_map,
            "mech_audit": {"overview": violations}}


def finalize_node(state: GraphFlowState) -> dict:
    from real_estate.graphflow.report import finalize_report
    report = finalize_report(
        _get_driver(), state["asset_id"], state["sections"], state["overview"],
        state["index_map"], state["evidence_by_axis"], state["mech_audit"],
        out_dir=state["out_dir"])
    return {"report": report}


# ───────────────────────────────────────────────────────────
# 그래프 조립
# ───────────────────────────────────────────────────────────
def build_report_graph():
    g = StateGraph(GraphFlowState)
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
    driver: Driver,
    *,
    asset_id: str,
    out_dir: str,
    db_path: str = "",
    location=None,
    expand_rounds: int = 0,
    expand_budget: int = 40,
    evidence_k: int = 12,
) -> GraphFlowState:
    """워크플로우 1회 실행. Langfuse 콜백 자동 부착."""
    global _driver
    _driver = driver

    from real_estate.graphflow.llm import get_langfuse_handler
    callbacks = [h for h in [get_langfuse_handler()] if h is not None]

    app = build_report_graph()
    initial: GraphFlowState = {
        "asset_id": asset_id, "out_dir": out_dir, "db_path": db_path,
        "location": location, "expand_rounds": expand_rounds,
        "expand_budget": expand_budget, "evidence_k": evidence_k,
        "collected_by": make_run_id("exp"),
    }
    final = app.invoke(initial, config={"callbacks": callbacks})
    logger.info("workflow 완료 — report=%s",
                (final.get("report") or {}).get("report_id"))
    return final
