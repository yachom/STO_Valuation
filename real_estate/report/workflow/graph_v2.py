"""v2 multi-agent topology — analyst 2-step + critic 루프 + final editor.

planner → 3 v2 analyst (parallel) → synthesizer → risk_critic
  → (issues & iter<2) reviser → synthesizer (loop back)
  → (pass) final_editor → END
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from real_estate.report.agents._analyst_v2 import make_v2_analyst_node
from real_estate.report.agents.base import AgentContext
from real_estate.report.agents.final_editor import final_editor_node
from real_estate.report.agents.planner import planner_node
from real_estate.report.agents.reviser_v2 import reviser_v2_node
from real_estate.report.agents.risk_critic import risk_critic_node
from real_estate.report.agents.synthesizer import synthesizer_node
from real_estate.report.agents.validator import validator_node
from real_estate.report.workflow.state_v2 import ReportStateV2

logger = logging.getLogger("workflow.v2")

_MAX_CRITIC_ITER = 2


def build_draft_graph_v2(ctx: AgentContext, *, db_path: str):
    g = StateGraph(ReportStateV2)

    # ── nodes ──
    g.add_node("planner", planner_node)

    tenant = make_v2_analyst_node("tenant", "테넌트 가치", db_path)
    esg = make_v2_analyst_node("esg", "ESG 가치", db_path)
    commercial = make_v2_analyst_node("commercial", "상권 가치", db_path)
    g.add_node("tenant_analyst", lambda s: tenant(s, ctx))
    g.add_node("esg_analyst", lambda s: esg(s, ctx))
    g.add_node("commercial_analyst", lambda s: commercial(s, ctx))

    g.add_node("synthesizer", synthesizer_node)
    g.add_node("risk_critic", risk_critic_node)
    g.add_node("reviser_v2", reviser_v2_node)
    g.add_node("validator", validator_node)
    g.add_node("final_editor", final_editor_node)

    # ── edges ──
    g.set_entry_point("planner")
    g.add_edge("planner", "tenant_analyst")
    g.add_edge("planner", "esg_analyst")
    g.add_edge("planner", "commercial_analyst")
    g.add_edge("tenant_analyst", "synthesizer")
    g.add_edge("esg_analyst", "synthesizer")
    g.add_edge("commercial_analyst", "synthesizer")
    g.add_edge("synthesizer", "risk_critic")

    # conditional: critic pass or iter limit reached → final_editor; else reviser
    def critic_router(state: dict) -> str:
        critic = state.get("critic_output", {})
        iter_count = state.get("critic_iter", 0)
        if critic.get("overall_pass") or iter_count >= _MAX_CRITIC_ITER:
            logger.info("critic pass=%s iter=%d → final_editor",
                        critic.get("overall_pass"), iter_count)
            return "final_editor"
        logger.info("critic pass=%s iter=%d → reviser_v2 loop",
                    critic.get("overall_pass"), iter_count)
        return "reviser_v2"

    g.add_conditional_edges("risk_critic", critic_router, {
        "final_editor": "final_editor",
        "reviser_v2": "reviser_v2",
    })
    g.add_edge("reviser_v2", "synthesizer")  # loop back
    g.add_edge("final_editor", "validator")
    g.add_edge("validator", END)

    return g.compile()


def state_to_report_sections_v2(state: dict) -> list[dict]:
    """ReportState → 섹션 리스트 (overview + tenant + esg + commercial 순서)."""
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
            "confidence_step1": sec.get("confidence_step1"),
            "confidence_final": sec.get("confidence_final"),
            "scout_calls_made": sec.get("scout_calls_made", []),
        })
    return out
