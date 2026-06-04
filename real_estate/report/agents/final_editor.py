"""Final Editor — 최종 보고서 일관성/가독성 polish. (Opus)

risk_critic이 overall_pass=True를 낸 뒤 한 번만 호출된다. 사실 변경은 금지이고
어조/문단 흐름/제목 일관성만 조정.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import ModelTier, build_llm

logger = logging.getLogger("agent.final_editor")


class PolishedSection(BaseModel):
    section_id: str
    polished_markdown: str = Field(description="가독성 향상 후 markdown. 사실/숫자/인용은 보존.")


class FinalEditorOutput(BaseModel):
    sections: list[PolishedSection]


_SYSTEM = korean_system_prompt(
    "최종 편집자. 사실/숫자/[E#]/[S#] 인용은 절대 변경 금지. 어조 일관성 + "
    "문단 흐름 + 중복 표현 제거만. 분량은 줄이지 마라."
)


def final_editor_node(state: dict) -> dict:
    from real_estate.report.tracing import emit
    emit("node_start", node="final_editor",
         sections_count=len(state.get("sections", {})))
    sections = state.get("sections", {})
    if not sections:
        emit("node_end", node="final_editor", status="skip", reason="no_sections")
        return {}

    inputs = []
    for sid, sec in sections.items():
        inputs.append(
            f"=== section_id={sid} | [{sec['axis']}] {sec['title']} ===\n"
            f"{sec['body_markdown']}"
        )

    user = (
        "아래 섹션들의 어조와 문단 흐름을 다듬으세요. 사실/숫자/인용 보존.\n\n"
        + "\n\n".join(inputs)
        + "\n\n각 section_id별로 polished_markdown을 반환하세요."
    )
    llm = build_llm(ModelTier.OPUS).with_structured_output(FinalEditorOutput)
    out: FinalEditorOutput = llm.invoke([
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ])

    new_sections = dict(sections)
    for ps in out.sections:
        if ps.section_id in new_sections:
            sec = dict(new_sections[ps.section_id])
            sec["body_markdown"] = ps.polished_markdown
            sec["polished"] = True
            new_sections[ps.section_id] = sec
    logger.info("final_editor polished %d sections", len(out.sections))
    emit("node_end", node="final_editor", status="ok",
         polished_sections=len(out.sections))
    return {"sections": new_sections}
