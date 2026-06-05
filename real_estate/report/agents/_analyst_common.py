"""축별 analyst의 공통 구현. tenant/esg/commercial 각각이 이 함수를 호출.

각 analyst는:
    1. section_outline에서 자기 axis의 section을 찾는다
    2. Retriever로 evidence 모은다
    3. LLM(Sonnet)으로 섹션 본문 + 인용 마크 생성
    4. state.sections[section_id] 갱신
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import AgentContext, format_snippets, korean_system_prompt
from real_estate.report.llm import get_llm
from real_estate.report.rag.protocol import Axis

logger = logging.getLogger("agent.analyst")


class AnalystOutput(BaseModel):
    body_markdown: str = Field(
        description="섹션 본문 markdown. 각 주장 끝에 [E#] 형식으로 evidence 인용을 붙인다."
    )
    used_evidence_indexes: list[int] = Field(
        description="본문에서 실제 인용한 evidence 인덱스 리스트 (E1=1, E2=2, ...)."
    )


def make_analyst_node(axis: Axis, role_label: str):
    """axis별 LangGraph 노드 함수를 만들어 반환."""

    system = korean_system_prompt(
        f"{role_label} 분석가. 주어진 evidence만 사용해 섹션 본문 markdown을 작성한다. "
        f"각 주장 끝에 [E#] 인덱스로 출처를 표기한다."
    )

    def node(state: dict, ctx: AgentContext) -> dict:
        outline = state.get("section_outline", [])
        my_section = next((s for s in outline if s["axis"] == axis), None)
        if my_section is None:
            logger.warning("axis=%s 섹션이 outline에 없음", axis)
            return {}

        question = my_section["question"]
        snippets = ctx.retriever.retrieve(question, k=8, axis=axis)

        evidence_text = format_snippets(snippets)
        user = (
            f"이번 섹션 제목: {my_section['title']}\n"
            f"이번 섹션 핵심 질문: {question}\n\n"
            f"=== 가용 evidence ===\n{evidence_text}\n\n"
            "위 evidence만 활용해 본 자산의 해당 축 평가 섹션 본문을 작성하세요.\n"
            "- 3~6개 단락의 markdown\n"
            "- 각 주장 끝에 [E#] 형식 인용\n"
            "- evidence가 부족한 부분은 '데이터 부족' 으로 명시"
        )
        llm = get_llm(f"{axis}_analyst").with_structured_output(AnalystOutput)
        result: AnalystOutput = llm.invoke([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])

        evidence_node_refs: list[str] = []
        evidence_snippets: list[str] = []
        for idx in result.used_evidence_indexes:
            if 1 <= idx <= len(snippets):
                evidence_node_refs.extend(snippets[idx - 1].node_refs)
                evidence_snippets.append(snippets[idx - 1].text)

        sections = dict(state.get("sections", {}))
        sections[my_section["section_id"]] = {
            "axis": axis,
            "title": my_section["title"],
            "body_markdown": result.body_markdown,
            "evidence_node_refs": list(dict.fromkeys(evidence_node_refs)),
            "evidence_snippets": evidence_snippets,
        }
        return {"sections": sections}

    return node
