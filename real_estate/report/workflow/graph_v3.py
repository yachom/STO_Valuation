"""graph_v3 — Co-STORM 식 planner_v3(moderator) ↔ analyst 다회차 루프.

planner_v3(initial) → 3 analyst(병렬) → synthesizer → risk_critic
    ├─ pass OR replan_iter ≥ MAX → final_editor → validator → END
    └─ fail (replan_iter < MAX) → reflection → planner_v3(replan)
        → 3 analyst(병렬 재호출) → synthesizer (loop)

graph_v2와 비교:
    - planner → planner_v3 (mind_map + replan 모드 dispatch)
    - reviser_v2 자리에 reflection (verbal memo) + planner_v3 replan
    - 단일 planner 노드가 initial/replan 두 모드를 자체 dispatch
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from real_estate.report.agents._analyst_v2 import make_v2_analyst_node
from real_estate.report.agents.base import AgentContext
from real_estate.report.agents.final_editor import final_editor_node
from real_estate.report.agents.planner_v3 import make_planner_v3_node
from real_estate.report.agents.reflection import reflection_node
from real_estate.report.agents.risk_critic import risk_critic_node
from real_estate.report.agents.synthesizer import synthesizer_node
from real_estate.report.agents.validator import validator_node
from real_estate.report.workflow.state_v3 import ReportStateV3

logger = logging.getLogger("workflow.v3")

_MAX_REPLAN = 2


def build_draft_graph_v3(ctx: AgentContext, *, db_path: str):
    g = StateGraph(ReportStateV3)

    # ── nodes ──
    g.add_node("planner_v3", make_planner_v3_node(db_path))

    tenant = make_v2_analyst_node("tenant", "테넌트 가치", db_path)
    esg = make_v2_analyst_node("esg", "ESG 가치", db_path)
    commercial = make_v2_analyst_node("commercial", "상권 가치", db_path)
    g.add_node("tenant_analyst", lambda s: tenant(s, ctx))
    g.add_node("esg_analyst", lambda s: esg(s, ctx))
    g.add_node("commercial_analyst", lambda s: commercial(s, ctx))

    g.add_node("synthesizer", synthesizer_node)
    g.add_node("risk_critic", risk_critic_node)
    g.add_node("reflection", reflection_node)
    g.add_node("final_editor", final_editor_node)
    g.add_node("validator", validator_node)

    # ── edges ──
    g.set_entry_point("planner_v3")
    g.add_edge("planner_v3", "tenant_analyst")
    g.add_edge("planner_v3", "esg_analyst")
    g.add_edge("planner_v3", "commercial_analyst")
    g.add_edge("tenant_analyst", "synthesizer")
    g.add_edge("esg_analyst", "synthesizer")
    g.add_edge("commercial_analyst", "synthesizer")
    g.add_edge("synthesizer", "risk_critic")

    def critic_router(state: dict) -> str:
        critic = state.get("critic_output") or {}
        replan_iter = state.get("replan_iter", 0)
        if critic.get("overall_pass") or replan_iter >= _MAX_REPLAN:
            logger.info("critic pass=%s replan_iter=%d → final_editor",
                        critic.get("overall_pass"), replan_iter)
            return "final_editor"
        logger.info("critic pass=%s replan_iter=%d → reflection → planner_v3",
                    critic.get("overall_pass"), replan_iter)
        return "reflection"

    g.add_conditional_edges("risk_critic", critic_router, {
        "final_editor": "final_editor",
        "reflection": "reflection",
    })
    g.add_edge("reflection", "planner_v3")  # loop back
    g.add_edge("final_editor", "validator")
    g.add_edge("validator", END)

    return g.compile()


def state_to_report_sections_v3(state: dict) -> list[dict]:
    """ReportStateV3 → 섹션 리스트 (overview + tenant + esg + commercial)."""
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
