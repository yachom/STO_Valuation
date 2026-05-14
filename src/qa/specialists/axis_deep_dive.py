"""AxisDeepDive: 특정 축의 plan/tool_calls/score 근거를 deep-dive."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ...agents._llm import make_llm
from ..session import QASession

SYSTEM = """당신은 부동산 STO 평가의 특정 축(ESG/인지도/상권·입지)의 채점 근거를 설명하는 전문가다.
플래너 가설, 리트리버가 호출한 도구 결과들, 그리고 스코어러의 rationale를 모두 받는다.

원칙:
- 점수·등급·강점·약점·리스크를 구체적 도구 호출 결과를 인용해 설명하라.
- '왜 이 점수인지'를 명확히 풀어주라.
- 한국어. 표/번호 목록 적극 사용.
"""

_AXIS_MAP = {
    "esg": "esg",
    "환경": "esg",
    "ESG": "esg",
    "인지도": "awareness",
    "awareness": "awareness",
    "상권": "vitality",
    "입지": "vitality",
    "vitality": "vitality",
}


def detect_axis(question: str) -> str | None:
    for k, v in _AXIS_MAP.items():
        if k in question or k.lower() in question.lower():
            return v
    return None


def run(question: str, session: QASession, callbacks: list[Any] | None = None) -> str:
    axis = detect_axis(question) or "esg"
    axis_data = session.axis(axis)
    if not axis_data:
        return f"'{axis}' 축 데이터를 찾을 수 없습니다."

    # 토큰 절약: tool_calls는 도구명 + 내용 200자만
    slim_calls = [
        {"tool": tc.get("tool"), "content": (tc.get("content") or "")[:600]}
        for tc in axis_data.get("tool_calls", [])
    ]
    deep = {
        "axis": axis_data.get("axis"),
        "axis_label": axis_data.get("axis_label"),
        "plan": axis_data.get("plan"),
        "score": axis_data.get("score"),
        "grade": axis_data.get("grade"),
        "strengths": axis_data.get("strengths"),
        "weaknesses": axis_data.get("weaknesses"),
        "risks": axis_data.get("risks"),
        "rationale": axis_data.get("rationale"),
        "citations": axis_data.get("citations"),
        "tool_calls": slim_calls,
        "retrieved_summary": axis_data.get("retrieved_summary"),
    }

    llm = make_llm(max_tokens=1800)
    body = (
        f"# 축: {axis.upper()}\n\n"
        f"# Deep dive 자료\n```json\n{json.dumps(deep, ensure_ascii=False, default=str)[:36000]}\n```\n\n"
        f"# 대화 히스토리\n{session.history_excerpt() or '(없음)'}\n\n"
        f"# 질문\n{question}"
    )
    resp = llm.invoke(
        [SystemMessage(content=SYSTEM), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    return resp.content if isinstance(resp.content, str) else str(resp.content)
