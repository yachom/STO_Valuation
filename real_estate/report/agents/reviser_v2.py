"""Reviser v2 — risk_critic의 issue들을 받아 해당 섹션을 수정. (Sonnet)

각 issue.location 섹션을 issue.suggestion 가이드를 보며 다시 작성.
overview는 synthesizer가 별도로 다시 만들도록 그대로 둠.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import ModelTier, build_llm

logger = logging.getLogger("agent.reviser_v2")


class RevisedSection(BaseModel):
    section_id: str
    body_markdown: str = Field(description="수정된 본문. [E#]/[S#] 인용 보존.")


class ReviserOutput(BaseModel):
    sections: list[RevisedSection]


_SYSTEM = korean_system_prompt(
    "리뷰어 issue를 충실히 반영해 섹션을 수정하는 reviser. 사실은 추가 발명하지 말고, "
    "이미 인용된 evidence + scout 결과 안에서 표현/논리/구조만 조정."
)


def reviser_v2_node(state: dict) -> dict:
    sections = state.get("sections", {})
    critic = state.get("critic_output", {})
    if not sections or not critic or critic.get("overall_pass"):
        return {}

    issues = critic.get("issues", [])
    target_locations = {it.get("location") for it in issues if it.get("location") != "overview"}
    if not target_locations:
        return {}

    # 섹션 ID는 outline의 section_id, location은 title 또는 axis로 들어올 수 있음
    # 단순화: 모든 sections를 LLM에 보내되 issue가 있는 location만 손대게 지시
    inputs = []
    for sid, sec in sections.items():
        inputs.append(
            f"=== section_id={sid} | axis={sec['axis']} | title={sec['title']} ===\n"
            f"{sec['body_markdown']}"
        )

    issue_lines = []
    for i, it in enumerate(issues):
        issue_lines.append(
            f"- [{it.get('severity')}/{it.get('kind')}] "
            f"location='{it.get('location')}': {it.get('description')} "
            f"→ 가이드: {it.get('suggestion')}"
        )

    user = (
        "다음은 보고서 critic의 issue 리스트입니다:\n\n"
        + "\n".join(issue_lines)
        + "\n\n수정 대상 섹션들:\n\n"
        + "\n\n".join(inputs)
        + "\n\n각 issue를 가이드대로 반영해 해당 섹션 본문을 다시 쓰세요. "
          "수정하지 않을 섹션은 결과에 포함하지 마세요 (issue가 없으면 그대로)."
    )
    llm = build_llm(ModelTier.SONNET).with_structured_output(ReviserOutput)
    try:
        out: ReviserOutput = llm.invoke([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ])
    except Exception as exc:
        logger.warning("reviser_v2 LLM 실패 — 섹션 변경 없이 패스: %s", exc)
        return {}

    new_sections = dict(sections)
    for rs in out.sections:
        if rs.section_id in new_sections:
            sec = dict(new_sections[rs.section_id])
            sec["body_markdown"] = rs.body_markdown
            sec["revised"] = True
            new_sections[rs.section_id] = sec
    logger.info("reviser_v2 updated %d sections", len(out.sections))
    return {"sections": new_sections}
