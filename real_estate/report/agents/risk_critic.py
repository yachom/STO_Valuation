"""Risk Critic — 통합된 보고서에서 모순/누락/과추정을 찾아내는 비평가. (Sonnet)

synthesizer가 만든 보고서를 한 번 더 비판적으로 읽고 issue list를 반환한다.
issue가 있으면 workflow가 reviser → synthesizer 루프를 한 번 더 돈다.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import korean_system_prompt
from real_estate.report.llm import get_llm
from real_estate.report.tracing import emit, node_scope

logger = logging.getLogger("agent.risk_critic")


class CriticIssue(BaseModel):
    severity: str = Field(description="low / medium / high")
    kind: str = Field(description=(
        "contradiction(축 간 모순) / missing(중요 누락) / overreach(과한 추정) / "
        "stale(오래된 데이터에 의존) / structural(섹션 구성 문제) 중 하나"
    ))
    location: str = Field(description="문제가 있는 섹션 이름 또는 'overview'")
    description: str = Field(description="구체적으로 무엇이 문제인지 1~2 문장")
    suggestion: str = Field(description="reviser/synthesizer에게 줄 수정 가이드")


class CriticOutput(BaseModel):
    overall_pass: bool = Field(
        description="이 보고서가 추가 수정 없이도 final로 갈 만한가?"
    )
    issues: list[CriticIssue] = Field(default_factory=list)


_SYSTEM = korean_system_prompt(
    "보고서 critic. 정밀 검토자 역할. 잘 쓰여진 부분은 침묵하고 문제만 짚는다. "
    "issues는 actionable해야 한다 — '명확하지 않다'가 아니라 '어떻게 명확하게 해라'. "
    "severity 정확히 구분: high=사실/위계 오류·핵심 누락, medium=구조·표현 미흡, "
    "low=가벼운 보완. high가 1개도 없고 medium도 2개 이하면 overall_pass=True."
)


def risk_critic_node(state: dict) -> dict:
    cur_iter = state.get("critic_iter", 0)
    with node_scope("risk_critic", iter=cur_iter,
                    sections_count=len(state.get("sections", {}))):
        sections = state.get("sections", {})
        overview = state.get("overview_section", {})
        if not sections:
            emit("critic_result", status="empty", overall_pass=True)
            return {
                "critic_output": {"overall_pass": True, "issues": []},
                "critic_iter": cur_iter + 1,
            }

        asset_summary = state.get("asset_summary", "")
        bundle = [f"=== Asset ===\n{asset_summary}\n"]
        if overview:
            bundle.append(f"=== Overview ===\n{overview.get('body_markdown','')}\n")
        for sid, sec in sections.items():
            bundle.append(
                f"=== [{sec['axis']}] {sec['title']} "
                f"(conf={sec.get('confidence_final','?')}) ===\n"
                f"{sec['body_markdown']}\n"
            )

        user = (
            "다음 보고서를 critic 관점으로 검토하세요.\n\n"
            + "\n".join(bundle)
            + "\n출력 형태로 issue들을 나열하세요. 큰 문제가 없으면 overall_pass=True, "
              "issues=[]. high가 1개 이상 또는 medium이 3개 이상이면 overall_pass=False. "
              "low 이슈만 있는 경우는 overall_pass=True (사소한 보완은 final_editor가 처리)."
        )
        llm = get_llm("risk_critic").with_structured_output(CriticOutput)
        out: CriticOutput = llm.invoke([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ])

        sev_order = {"high": 0, "medium": 1, "low": 2}
        sorted_issues = sorted(
            out.issues,
            key=lambda it: sev_order.get((it.severity or "").lower(), 9),
        )
        n_high = sum(1 for it in sorted_issues
                     if (it.severity or "").lower() == "high")
        n_medium = sum(1 for it in sorted_issues
                       if (it.severity or "").lower() == "medium")
        auto_pass = (n_high == 0 and n_medium <= 2)
        overall_pass = out.overall_pass or auto_pass

        result = out.model_copy(update={
            "overall_pass": overall_pass,
            "issues": sorted_issues,
        })
        logger.info(
            "risk_critic — pass=%s (raw_pass=%s, auto_pass=%s, h=%d m=%d total=%d)",
            overall_pass, out.overall_pass, auto_pass,
            n_high, n_medium, len(sorted_issues))
        emit("critic_result", status="ok",
             iter=cur_iter + 1,
             overall_pass=overall_pass,
             raw_pass=out.overall_pass,
             auto_pass=auto_pass,
             n_high=n_high, n_medium=n_medium,
             n_low=len(sorted_issues) - n_high - n_medium,
             issues_summary=[
                 {"severity": it.severity, "kind": it.kind,
                  "location": it.location, "desc": (it.description or "")[:120]}
                 for it in sorted_issues[:5]
             ])
        return {
            "critic_output": result.model_dump(),
            "critic_iter": cur_iter + 1,
        }
