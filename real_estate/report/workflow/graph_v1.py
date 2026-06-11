"""초안 작성 LangGraph — planner → 3 analysts (병렬) → synthesizer → validator → END."""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from real_estate.report.agents.base import AgentContext
from real_estate.report.agents.commercial_analyst import commercial_analyst_node
from real_estate.report.agents.esg_analyst import esg_analyst_node
from real_estate.report.agents.planner import make_planner_node
from real_estate.report.agents.synthesizer import synthesizer_node
from real_estate.report.agents.tenant_analyst import tenant_analyst_node
from real_estate.report.agents.validator import validator_node
from real_estate.report.workflow.state import ReportState


def build_draft_graph(ctx: AgentContext, *, db_path: str):
    """compile된 LangGraph 반환. 호출자가 invoke(state)로 사용."""
    g = StateGraph(ReportState)

    g.add_node("planner", make_planner_node(db_path))
    # analyst 노드들은 ctx를 closure로 잡아 wrap
    g.add_node("tenant_analyst", lambda s: tenant_analyst_node(s, ctx))
    g.add_node("esg_analyst", lambda s: esg_analyst_node(s, ctx))
    g.add_node("commercial_analyst", lambda s: commercial_analyst_node(s, ctx))
    g.add_node("synthesizer", synthesizer_node)
    g.add_node("validator", validator_node)

    g.set_entry_point("planner")
    # planner 끝나면 3 analyst를 병렬 실행 (fan-out)
    g.add_edge("planner", "tenant_analyst")
    g.add_edge("planner", "esg_analyst")
    g.add_edge("planner", "commercial_analyst")
    # 세 analyst가 모두 끝난 뒤 synthesizer (fan-in)
    g.add_edge("tenant_analyst", "synthesizer")
    g.add_edge("esg_analyst", "synthesizer")
    g.add_edge("commercial_analyst", "synthesizer")
    g.add_edge("synthesizer", "validator")
    g.add_edge("validator", END)

    return g.compile()


def state_to_report_sections(state: dict) -> list[dict]:
    """ReportState → ReportSection dict 리스트 (overview 우선 + axis 섹션들)."""
    out: list[dict] = []
    overview = state.get("overview_section")
    if overview:
        out.append(overview)
    axis_order = ["tenant", "esg", "commercial"]
    sections = state.get("sections", {})
    items = sorted(
        sections.items(),
        key=lambda kv: axis_order.index(kv[1].get("axis"))
        if kv[1].get("axis") in axis_order else 99,
    )
    for sid, sec in items:
        out.append({
            "axis": sec.get("axis"),
            "title": sec.get("title"),
            "body_markdown": sec.get("body_markdown"),
            "evidence_node_refs": sec.get("evidence_node_refs", []),
            "evidence_snippets": sec.get("evidence_snippets", []),
        })
    return out
