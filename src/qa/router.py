"""Supervisor Router: 어느 전문가를 부를지 결정."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..agents._llm import make_llm
from .session import QASession

SPECIALISTS = [
    "report_analyst",
    "graph_query",
    "axis_deep_dive",
    "live_tool",
    "composite_recalc",
    "fallback",
]

SYSTEM = """당신은 부동산 STO 평가 QA 시스템의 라우터다.
사용자 질문을 분석해 어떤 전문가에게 위임할지 정한다. 복수 선택 가능.

전문가 카탈로그:
- report_analyst: 보고서 본문 인용/요약/해설. "요약해줘", "보고서에 뭐라고 적혀있어?"
- graph_query: 뉴스 지식그래프 조회. "X에 대한 뉴스", "임차인", "관련 사건", "매각 이력"
- axis_deep_dive: 특정 축(ESG/인지도/상권) 점수 근거 deep-dive. "왜 ESG 점수가 X야?"
- live_tool: 새 도구 호출 필요. "반경 500m 카페 다시 세줘", "추가로 X 검색해줘"
- composite_recalc: 가중치 변경 시뮬레이션. "ESG 50%면 종합?"
- fallback: 위 모두에 해당 안 되는 일반 질문

출력은 JSON 한 개만:
{
  "specialists": ["..."],   // 호출할 전문가들. 1~3개.
  "parallel": true|false,    // 병렬 호출 가능한지
  "reasoning": "왜 이렇게 라우팅했는지 한 문장"
}
"""


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _heuristic(question: str) -> list[str]:
    """LLM 실패 시 키워드 기반 폴백."""
    q = question.lower()
    picks: list[str] = []
    if any(k in question for k in ["뉴스", "기사", "임차인", "매각", "관련 사건", "이슈", "소송", "리모델링"]):
        picks.append("graph_query")
    if any(k in question for k in ["왜", "근거", "이유", "어떻게", "ESG 점수", "인지도 점수", "상권 점수"]):
        picks.append("axis_deep_dive")
    if any(k in question for k in ["반경", "다시 세", "추가 검색", "주변", "근처"]) and "graph_query" not in picks:
        picks.append("live_tool")
    if any(k in question for k in ["가중치", "%", "퍼센트", "재계산"]):
        picks.append("composite_recalc")
    if any(k in question for k in ["요약", "보고서", "정리해줘", "한 줄"]):
        picks.append("report_analyst")
    return picks or ["fallback"]


def route(question: str, session: QASession, callbacks: list[Any] | None = None) -> dict[str, Any]:
    llm = make_llm(max_tokens=400)
    body = (
        f"# 대상 자산: {session.address_label()}\n"
        f"# 대화 히스토리\n{session.history_excerpt() or '(없음)'}\n\n"
        f"# 현재 질문\n{question}"
    )
    try:
        resp = llm.invoke(
            [SystemMessage(content=SYSTEM), HumanMessage(content=body)],
            config={"callbacks": callbacks or []},
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        text = _strip_fence(text)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(m.group(0)) if m else json.loads(text)
        picks = [s for s in (parsed.get("specialists") or []) if s in SPECIALISTS]
        if not picks:
            picks = _heuristic(question)
        return {
            "specialists": picks,
            "parallel": bool(parsed.get("parallel", True)),
            "reasoning": parsed.get("reasoning", ""),
        }
    except Exception as exc:
        return {
            "specialists": _heuristic(question),
            "parallel": True,
            "reasoning": f"router LLM failed ({type(exc).__name__}): heuristic 사용",
        }
