"""Synthesizer — 3축 섹션을 통합해 전체 보고서 + executive summary 생성. (Opus)"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import ModelTier, build_llm

logger = logging.getLogger("agent.synthesizer")


class SynthesizerOutput(BaseModel):
    executive_summary_markdown: str = Field(
        description="자산 전체에 대한 5~10문장 executive summary. 3축의 핵심 발견을 통합."
    )
    cross_axis_insights_markdown: str = Field(
        description="축 간 상호작용/리스크/기회를 2~4단락으로 정리."
    )


_SYSTEM = korean_system_prompt(
    "보고서 통합자. 3개 축의 분석 섹션을 통합해 전체 executive summary와 "
    "축 간 cross-axis insight를 작성한다. 모든 진술은 섹션 본문에 등장한 근거에 기반."
)


def synthesizer_node(state: dict) -> dict:
    sections = state.get("sections", {})
    if not sections:
        logger.warning("synthesizer: 섹션이 없음")
        return {}

    parts = [f"자산: {state.get('asset_summary', '')}\n"]
    for sid, sec in sections.items():
        parts.append(f"\n## [{sec['axis']}] {sec['title']}\n{sec['body_markdown']}\n")
    bundle = "".join(parts)

    user = (
        f"=== 축별 섹션 ===\n{bundle}\n\n"
        "위 섹션들을 종합해 5~10문장 executive summary와 cross-axis insight를 작성하세요. "
        "원 섹션 본문에 등장한 사실 외엔 추가하지 마세요."
    )
    llm = build_llm(ModelTier.OPUS).with_structured_output(SynthesizerOutput)
    result: SynthesizerOutput = llm.invoke([
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ])

    overview = {
        "axis": "overview",
        "title": "Executive Summary & 교차 축 통찰",
        "body_markdown": (
            f"### Executive Summary\n\n{result.executive_summary_markdown}\n\n"
            f"### Cross-axis Insights\n\n{result.cross_axis_insights_markdown}\n"
        ),
        "evidence_node_refs": [],
        "evidence_snippets": [],
    }
    return {"overview_section": overview}
