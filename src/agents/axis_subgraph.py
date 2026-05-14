"""Planner → Retriever(ReAct) → Scorer 서브그래프.

각 축은 동일 구조를 공유하며 system prompt와 axis 라벨만 다르다.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from .. import config
from ..kg.graph_store import KnowledgeGraph
from ._llm import make_llm
from .tools_lc import build_tools


class AxisState(TypedDict, total=False):
    axis: str
    axis_label: str
    plan: dict[str, Any]
    retrieved: list[dict[str, Any]]
    scored: dict[str, Any]


PLANNER_SYSTEM = """당신은 부동산 STO 사회적 가치 평가의 한 축을 담당하는 시니어 리서치 플래너다.
주어진 축에 대해 어떤 데이터를 어떤 도구로 어떤 인자로 수집할지 계획을 짠다.

출력은 JSON 객체 한 개:
{
  "axis": "...",
  "hypothesis": "이 자산의 해당 축이 강점인지 약점인지에 대한 초기 가설",
  "must_check": [
    {"tool": "tool_name", "args": {...}, "why": "이 호출이 왜 필요한지"}
  ],
  "follow_up_hints": ["조건부로 추가 호출할 만한 단서들"]
}

권장:
- 'graph_db_axis_evidence'는 뉴스 지식그래프 신호를 끌어오니 반드시 포함하라.
- 'get_subject_metadata'는 보통 첫 호출로 포함하라.
- 인지도 축이면 'naver_web_search' 'naver_keyword_volume' 'kakao_keyword_places'를 후보로.
- ESG 축이면 'gseed_lookup' 'get_subject_metadata' + 그래프.
- 상권/입지 축이면 'kakao_poi_radius' 'sbiz_stores_radius' 'molit_transactions' + 그래프.
"""


RETRIEVER_SYSTEM_TMPL = """당신은 부동산 STO 평가의 '{axis_label}' 축을 위한 데이터 수집 에이전트다.
플래너의 계획을 기반으로 사용할 도구를 호출해 충분한 근거를 수집하라.

원칙:
- 플래너 계획의 must_check 호출을 먼저 수행하라.
- 결과가 빈약/모호하면 인자 변형(반경, 키워드)으로 1~2회 추가 시도.
- 정량/정성 양쪽 근거를 모으는 것을 목표로 하라.
- 같은 도구를 같은 인자로 반복 호출하지 말라.
- 최대 8회 정도 도구 호출 후 종료. 마지막 응답에 'DONE'을 포함하라.
"""


SCORER_SYSTEM_TMPL = """당신은 부동산 STO 평가의 '{axis_label}' 축 채점자다.
수집된 도구 호출 결과를 종합해 평가하라. 보수적이고 근거 기반으로.

출력은 반드시 JSON:
{{
  "score": 0~100,
  "grade": "A+|A|B+|B|C+|C|D",
  "strengths": ["..."],
  "weaknesses": ["..."],
  "risks": ["..."],
  "rationale": "한국어 5~10문장 근거 요약",
  "citations": ["주요 근거 도구 호출 결과나 뉴스 출처"]
}}

데이터가 결측되면 50점 기본, 강한 신호만큼만 가감.
"""


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return m.group(1).strip() if m else text


def _parse_json(text: str) -> dict[str, Any]:
    text = _strip_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _planner(axis_label: str, metadata: dict[str, Any], kg_summary: dict[str, Any], callbacks: list[Any] | None) -> dict[str, Any]:
    llm = make_llm(max_tokens=1500)
    addr = metadata.get("address", {})
    body = (
        f"# 축: {axis_label}\n\n"
        f"# 대상 자산 요약\n```json\n{json.dumps({'address': addr, 'identifiers': metadata.get('identifiers')}, ensure_ascii=False)}\n```\n\n"
        f"# 지식그래프 현황 (뉴스 기반)\n```json\n{json.dumps(kg_summary, ensure_ascii=False)}\n```\n\n"
        f"위 정보를 바탕으로 수집 계획을 JSON으로 출력하라."
    )
    resp = llm.invoke(
        [SystemMessage(content=PLANNER_SYSTEM), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    return _parse_json(text) or {"axis": axis_label, "must_check": []}


def _retriever(axis_label: str, plan: dict[str, Any], tools, callbacks: list[Any] | None, max_iterations: int = 8) -> dict[str, Any]:
    """ReAct 에이전트로 도구 호출 루프."""
    llm = make_llm(max_tokens=2000)
    system = RETRIEVER_SYSTEM_TMPL.format(axis_label=axis_label)

    agent = create_react_agent(llm, tools)

    user = (
        f"플래너 계획:\n```json\n{json.dumps(plan, ensure_ascii=False)}\n```\n\n"
        "이제 도구를 호출해 근거를 수집하라."
    )
    messages = [SystemMessage(content=system), HumanMessage(content=user)]

    result = agent.invoke(
        {"messages": messages},
        config={
            "callbacks": callbacks or [],
            "recursion_limit": max_iterations * 4,
            "run_name": f"retriever-{axis_label}",
        },
    )

    final_messages = result.get("messages", [])
    tool_calls: list[dict[str, Any]] = []
    final_text: str = ""

    for msg in final_messages:
        if isinstance(msg, ToolMessage):
            tool_calls.append({"tool": msg.name, "content": str(msg.content)[:4000]})
        elif isinstance(msg, AIMessage):
            if isinstance(msg.content, str):
                final_text = msg.content

    return {"tool_calls": tool_calls, "final_text": final_text, "iterations": len([m for m in final_messages if isinstance(m, ToolMessage)])}


def _scorer(axis_label: str, plan: dict[str, Any], retrieved: dict[str, Any], callbacks: list[Any] | None) -> dict[str, Any]:
    llm = make_llm(max_tokens=2000)
    system = SCORER_SYSTEM_TMPL.format(axis_label=axis_label)
    body = (
        f"# 플래너 가설\n{plan.get('hypothesis', '(없음)')}\n\n"
        f"# 수집 결과 ({len(retrieved.get('tool_calls', []))}건 도구 호출)\n"
        f"```json\n{json.dumps(retrieved.get('tool_calls', []), ensure_ascii=False, default=str)[:60000]}\n```\n\n"
        f"# 리트리버 최종 코멘트\n{retrieved.get('final_text', '')[:2000]}\n"
    )
    resp = llm.invoke(
        [SystemMessage(content=system), HumanMessage(content=body)],
        config={"callbacks": callbacks or []},
    )
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    return _parse_json(text) or {
        "score": None,
        "grade": "N/A",
        "strengths": [],
        "weaknesses": [],
        "risks": ["스코어 파싱 실패"],
        "rationale": text[:2000],
    }


def run_axis(
    axis: str,
    axis_label: str,
    metadata: dict[str, Any],
    kg: KnowledgeGraph | None,
    callbacks: list[Any] | None = None,
) -> dict[str, Any]:
    tools = build_tools(metadata, kg)
    kg_summary = kg.summary() if kg else {"nodes": 0, "edges": 0}
    plan = _planner(axis_label, metadata, kg_summary, callbacks)
    retrieved = _retriever(axis_label, plan, tools, callbacks)
    scored = _scorer(axis_label, plan, retrieved, callbacks)
    return {
        "axis": axis,
        "axis_label": axis_label,
        "plan": plan,
        "retrieved_summary": {
            "tool_call_count": len(retrieved.get("tool_calls", [])),
            "iterations": retrieved.get("iterations"),
            "final_text": retrieved.get("final_text"),
        },
        "tool_calls": retrieved.get("tool_calls", []),
        **scored,
    }
