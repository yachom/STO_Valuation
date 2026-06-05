"""Synthesizer — 3축 섹션을 통합해 전체 보고서 + executive summary 생성. (Opus)"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import cache_block, cached_system, get_llm
from real_estate.report.tracing import emit, node_scope

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
    with node_scope("synthesizer",
                    sections_count=len(state.get("sections", {}))):
        sections = state.get("sections", {})
        if not sections:
            logger.warning("synthesizer: 섹션이 없음")
            emit("synthesizer_result", status="no_sections")
            return {}

        # invariant prefix — asset_summary는 pipeline 안에서 동일 → cache
        invariant_prefix = (
            f"자산: {state.get('asset_summary', '')}\n=== 축별 섹션 ===\n"
        )
        # variable — sections는 reviser loop에서 변동
        section_parts = []
        for sid, sec in sections.items():
            section_parts.append(
                f"\n## [{sec['axis']}] {sec['title']}\n{sec['body_markdown']}\n")
        variable_part = (
            "".join(section_parts)
            + "\n\n위 섹션들을 종합해 5~10문장 executive summary와 cross-axis insight를 "
              "작성하세요. 원 섹션 본문에 등장한 사실 외엔 추가하지 마세요."
        )

        llm = get_llm("synthesizer").with_structured_output(SynthesizerOutput)
        result: SynthesizerOutput = llm.invoke([
            {"role": "system", "content": cached_system(_SYSTEM)},
            {"role": "user", "content": [
                cache_block(invariant_prefix),
                {"type": "text", "text": variable_part},
            ]},
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
        emit("synthesizer_result", status="ok",
             exec_summary_chars=len(result.executive_summary_markdown),
             cross_axis_chars=len(result.cross_axis_insights_markdown))
        return {"overview_section": overview}
