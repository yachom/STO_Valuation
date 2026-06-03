"""QA 에이전트 — 합의된 보고서 기반 질문 응답. (Sonnet 기본; 애매하면 Opus)

원칙:
    - retriever가 반환한 evidence 외엔 발언하지 않는다.
    - snippet 수 < k_min이면 "보고서 범위 밖" 응답으로 강제.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from real_estate.report.agents.base import format_snippets, korean_system_prompt
from real_estate.report.llm import ModelTier, build_llm
from real_estate.report.rag.protocol import Retriever

logger = logging.getLogger("agent.qa")


class QaOutput(BaseModel):
    answer_markdown: str = Field(
        description="질문에 대한 답변. 각 사실에 [E#] 인용 부착. 5문장 이내 권장."
    )
    used_evidence_indexes: list[int] = Field(
        description="실제 답변에 인용한 evidence 인덱스 리스트."
    )
    is_out_of_scope: bool = Field(
        description="질문이 보고서 범위를 벗어났다고 판단되면 True."
    )


@dataclass
class QaResult:
    answer_markdown: str
    citations: list[dict]    # [{e_index, snippet_text, section_id, axis, refs}]
    out_of_scope: bool


_SYSTEM = korean_system_prompt(
    "보고서 범위 안 QA 응답자. evidence에서 직접 끌어올 수 있는 정보로만 답한다. "
    "evidence가 부족하거나 무관하면 is_out_of_scope=True로 표시."
)

_K_MIN_FOR_ANSWER = 1


def answer_question(
    *,
    question: str,
    retriever: Retriever,
    k: int = 6,
) -> QaResult:
    snippets = retriever.retrieve(question, k=k)
    if len(snippets) < _K_MIN_FOR_ANSWER:
        return QaResult(
            answer_markdown="이 보고서 범위 밖의 질문입니다. 보고서가 인용한 내용에서 "
                            "이 질문에 답할 evidence를 찾지 못했습니다.",
            citations=[],
            out_of_scope=True,
        )

    user = (
        f"질문:\n{question}\n\n"
        f"=== 합의된 보고서가 인용한 evidence ===\n{format_snippets(snippets)}\n\n"
        "위 evidence만 사용해 질문에 답하세요. 5문장 이내. 각 사실 끝에 [E#] 인용. "
        "evidence가 질문과 무관하면 is_out_of_scope=True."
    )
    llm = build_llm(ModelTier.SONNET).with_structured_output(QaOutput)
    result: QaOutput = llm.invoke([
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ])

    citations = []
    for idx in result.used_evidence_indexes:
        if 1 <= idx <= len(snippets):
            s = snippets[idx - 1]
            citations.append({
                "e_index": idx,
                "snippet_text": s.text,
                "section_id": s.metadata.get("section_id"),
                "section_title": s.metadata.get("section_title"),
                "axis": s.metadata.get("axis"),
                "refs": s.node_refs,
            })

    return QaResult(
        answer_markdown=result.answer_markdown,
        citations=citations,
        out_of_scope=result.is_out_of_scope,
    )
