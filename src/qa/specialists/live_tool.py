"""LiveTool: 추가 도구 호출이 필요한 질문 (반경/검색/실거래 등) → ReAct."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from ...agents._llm import make_llm
from ...agents.tools_lc import build_tools
from ..session import QASession

SYSTEM = """당신은 부동산 STO 평가용 라이브 도구 호출 에이전트다.
사용자 질문에 답하기 위해 필요한 도구를 호출하라 (반경 POI, 키워드 검색, 실거래, 그래프 DB 등).

원칙:
- 같은 인자로 같은 도구를 반복 호출하지 말라.
- 최대 5회 도구 호출 후 답변. 마지막 응답은 한국어 자연어 답변.
- 인용 가능한 수치(반경 m, 카테고리 등)를 포함해 답하라.
"""


def run(question: str, session: QASession, callbacks: list[Any] | None = None) -> str:
    tools = build_tools(session.metadata(), session.kg)
    llm = make_llm(max_tokens=1500)
    agent = create_react_agent(llm, tools)

    messages = [
        SystemMessage(content=SYSTEM),
        HumanMessage(
            content=(
                f"대상 자산: {session.address_label()}\n"
                f"대화 히스토리: {session.history_excerpt() or '(없음)'}\n\n"
                f"질문: {question}"
            )
        ),
    ]
    result = agent.invoke(
        {"messages": messages},
        config={"callbacks": callbacks or [], "recursion_limit": 20, "run_name": "qa-live-tool"},
    )
    final_text = ""
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage) and isinstance(msg.content, str):
            final_text = msg.content
    return final_text or "답변을 생성하지 못했습니다."
