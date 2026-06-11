"""Reflection — critic 비판 + 직전 라운드 본문 요약 → 1단락 memo. (Sonnet)

Reflexion(Shinn et al., NeurIPS 2023)의 verbal feedback 골격.
critic_iter > 0 인 상태에서 호출되며, planner_v3 replan 모드의 입력이 된다.

입력 (state):
    critic_output     (필수 — overall_pass=False여야 의미 있음)
    sections          (직전 라운드 본문)
    prior_human_comments / prior_qa_gaps (참고만 — 이미 planner_v3 initial이 흡수했음)

출력 (state delta):
    reflection_memo: str   (planner_v3 replan이 읽음)

skip 조건:
    - critic_output.overall_pass == True
    - critic_output 자체가 비어 있음
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import cached_system, get_llm
from real_estate.report.tracing import emit, node_scope

logger = logging.getLogger("agent.reflection")


class ReflectionOutput(BaseModel):
    memo: str = Field(
        description=("<=200자 한국어 1단락. 형식: '잘된 것: ... / 부족한 것: ... / "
                     "다음 라운드 방침: 어느 axis에 무엇을 추가 retrieve/scout 해야 하는가'"),
    )


_SYSTEM = korean_system_prompt(
    "보고서 라운드 평가자 (Reflexion 식 verbal feedback). 직전 라운드의 본문과 critic 지적을 "
    "받아 1단락 memo로 압축한다. 잘된 것·부족한 것·다음 라운드 방침 3요소 모두 포함. "
    "구체적인 axis(tenant/esg/commercial) 이름과 'retrieve / scout 보강 필요' 같은 "
    "actionable 방침을 명시한다."
)


def reflection_node(state: dict) -> dict:
    """critic이 fail이면 verbal memo 생성, pass면 skip."""
    with node_scope("reflection",
                    critic_iter=state.get("critic_iter", 0),
                    replan_iter=state.get("replan_iter", 0)):
        critic = state.get("critic_output") or {}
        if not critic:
            emit("reflection_skip", reason="no_critic")
            return {}
        if critic.get("overall_pass"):
            emit("reflection_skip", reason="critic_pass")
            return {}

        all_issues = critic.get("issues") or []
        focused = [
            it for it in all_issues
            if (it.get("severity") or "").lower() in ("high", "medium")
        ]
        if not focused:
            emit("reflection_skip", reason="only_low_issues")
            return {}

        sections = state.get("sections") or {}
        sections_summary = "\n".join(
            f"- [{s.get('axis')}] {s.get('title','')} "
            f"(conf={s.get('confidence_final','?')}, "
            f"chars={len(s.get('body_markdown') or '')})"
            for s in sections.values()
        ) or "(섹션 없음)"

        issues_text = "\n".join(
            f"- [{i.get('severity')}/{i.get('kind')}] {i.get('location')}: "
            f"{i.get('description')} → {i.get('suggestion')}"
            for i in focused[:6]
        )

        user = (
            f"=== 직전 라운드 본문 요약 ===\n{sections_summary}\n\n"
            f"=== critic 지적 (high/medium) ===\n{issues_text}\n\n"
            "위를 1단락 한국어 memo로 압축. 잘된 것·부족한 것·다음 라운드 방침 3요소 모두 포함."
        )
        llm = get_llm("reflection").with_structured_output(ReflectionOutput)
        try:
            out: ReflectionOutput = llm.invoke([
                {"role": "system", "content": cached_system(_SYSTEM)},
                {"role": "user", "content": user},
            ])
        except Exception as exc:
            logger.warning("reflection LLM failed: %s — empty memo", exc)
            emit("reflection_result", status="llm_error", error=str(exc)[:200])
            return {"reflection_memo": ""}

        logger.info("reflection memo (%d chars): %s",
                    len(out.memo), out.memo[:80])
        emit("reflection_result",
             status="ok",
             memo_chars=len(out.memo),
             issues_focused=len(focused))
        return {"reflection_memo": out.memo}
