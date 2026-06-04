"""Planner — asset_summary를 받아 3축 섹션 outline을 생성. (Opus 등급)"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import ModelTier, build_llm

logger = logging.getLogger("agent.planner")


class SectionPlan(BaseModel):
    axis: str = Field(description="tenant / esg / commercial 중 하나")
    title: str = Field(description="섹션 제목 (15자 이내)")
    question: str = Field(description="해당 축에 대해 본 자산을 평가하기 위한 핵심 질문")


class PlannerOutput(BaseModel):
    sections: list[SectionPlan] = Field(
        description="정확히 3개의 섹션 — tenant, esg, commercial 각 1개씩."
    )


_SYSTEM = korean_system_prompt(
    "보고서 기획자. 자산 요약을 받아 3축(테넌트/ESG/상권)에 대해 평가 섹션을 "
    "각 1개씩 기획하고, 각 섹션의 핵심 질문을 만든다."
)


def planner_node(state: dict) -> dict:
    """asset_summary → section_outline (3개)."""
    from real_estate.report.tracing import emit
    emit("node_start", node="planner", input_keys=list(state.keys()))
    llm = build_llm(ModelTier.OPUS).with_structured_output(PlannerOutput)
    asset_summary = state.get("asset_summary", "")
    user = (
        f"자산 요약:\n{asset_summary}\n\n"
        "위 자산을 평가하기 위해 테넌트 가치 / ESG 가치 / 상권 가치 각 축마다 "
        "섹션 1개씩 기획해 주세요. 섹션마다 '이 단지에 대해 무엇을 알아내야 하는가'를 "
        "구체적 질문으로 표현해 주세요."
    )
    result: PlannerOutput = llm.invoke([
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ])

    outline = []
    for sp in result.sections:
        outline.append({
            "section_id": f"sec_{uuid.uuid4().hex[:8]}",
            "axis": sp.axis,
            "title": sp.title,
            "question": sp.question,
        })
    logger.info("planner produced %d sections", len(outline))
    emit("node_end", node="planner", sections=len(outline),
         outline=[{"axis": o["axis"], "title": o["title"], "question": o["question"]}
                  for o in outline])
    return {"section_outline": outline}
