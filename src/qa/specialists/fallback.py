"""Fallback: 다른 전문가가 처리하지 못하는 일반 질문."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ...agents._llm import make_llm
from ..session import QASession

SYSTEM = """당신은 부동산 STO 사회적 가치 평가 보고서에 대한 일반 질의응답 보조자다.
보고서 요약 정보와 자산 메타데이터 정도의 컨텍스트만 받는다.
모르는 것은 모른다고 말하고, 더 구체적 자료가 필요하면 어떤 질문이 좋을지 가이드하라.
한국어로 간결히 답하라."""


def run(question: str, session: QASession, callbacks: list[Any] | None = None) -> str:
    addr = session.metadata().get("address") or {}
    summary = {
        "asset": session.address_label(),
        "address": addr,
        "composite": session.composite(),
        "axis_scores": {k: session.axis(k).get("score") for k in ("esg", "awareness", "vitality")},
    }
    llm = make_llm(max_tokens=900)
    body = (
        f"# 컨텍스트\n{summary}\n\n"
        f"# 대화 히스토리\n{session.history_excerpt() or '(없음)'}\n\n"
        f"# 질문\n{question}"
    )
    resp = llm.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    return resp.content if isinstance(resp.content, str) else str(resp.content)
