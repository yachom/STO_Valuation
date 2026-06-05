"""Reviser v2 — risk_critic의 issue들을 받아 해당 섹션을 수정. (Sonnet)

각 issue.location 섹션을 issue.suggestion 가이드를 보며 다시 작성.
overview는 synthesizer가 별도로 다시 만들도록 그대로 둠.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import cached_system, get_llm
from real_estate.report.tracing import emit, node_scope

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
    with node_scope("reviser_v2",
                    critic_iter=state.get("critic_iter", 0),
                    critic_issues=len(
                        (state.get("critic_output") or {}).get("issues", []))):
        sections = state.get("sections", {})
        critic = state.get("critic_output", {})
        if not sections or not critic or critic.get("overall_pass"):
            emit("reviser_result", status="skip",
                 reason="critic_pass_or_empty")
            return {}

        all_issues = critic.get("issues", [])
        issues = [
            it for it in all_issues
            if (it.get("severity") or "").lower() in ("high", "medium")
        ]
        high_issues = [it for it in issues
                       if (it.get("severity") or "").lower() == "high"]
        if not issues:
            logger.info("reviser_v2: low 이슈만 있어 skip — final_editor가 처리")
            emit("reviser_result", status="skip", reason="only_low_issues")
            return {}

        target_locations = {it.get("location") for it in issues
                            if it.get("location") != "overview"}
        if not target_locations:
            emit("reviser_result", status="skip", reason="overview_only")
            return {}

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

        instruction = (
            "각 issue를 가이드대로 반영해 해당 섹션 본문을 다시 쓰세요. "
            "수정하지 않을 섹션은 결과에 포함하지 마세요 (issue가 없으면 그대로). "
        )
        if high_issues:
            instruction += (
                f"high 이슈 {len(high_issues)}건은 반드시 반영. "
                "medium은 자연스럽게 통합되면 반영하되, 분량을 늘리지 마라."
            )
        else:
            instruction += "medium 이슈만 있으므로 가벼운 수정 (한두 단락 보강)으로 충분."

        user = (
            "다음은 보고서 critic의 issue 리스트입니다 (severity high/medium만 전달):\n\n"
            + "\n".join(issue_lines)
            + "\n\n수정 대상 섹션들:\n\n"
            + "\n\n".join(inputs)
            + "\n\n" + instruction
        )
        llm = get_llm("reviser_v2").with_structured_output(ReviserOutput)
        try:
            out: ReviserOutput = llm.invoke([
                {"role": "system", "content": cached_system(_SYSTEM)},
                {"role": "user", "content": user},
            ])
        except Exception as exc:
            logger.warning("reviser_v2 LLM 실패 — 섹션 변경 없이 패스: %s", exc)
            emit("reviser_result", status="llm_error", error=str(exc)[:200])
            return {}

        new_sections = dict(sections)
        for rs in out.sections:
            if rs.section_id in new_sections:
                sec = dict(new_sections[rs.section_id])
                sec["body_markdown"] = rs.body_markdown
                sec["revised"] = True
                new_sections[rs.section_id] = sec
        logger.info("reviser_v2 updated %d sections", len(out.sections))
        emit("reviser_result", status="ok",
             updated_sections=len(out.sections),
             processed_high=len(high_issues),
             processed_medium=len(issues) - len(high_issues))
        return {"sections": new_sections}
