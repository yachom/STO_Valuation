"""Reviser — 섹션 본문 + 리뷰어 코멘트 묶음 → 수정된 섹션 본문. (Sonnet)

LangGraph interrupt() 대신 직접 함수 호출. Streamlit 핸들러에서 호출되어
새 ReportVersion으로 저장된다.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from real_estate.report.agents.base import format_snippets, korean_system_prompt
from real_estate.report.llm import get_llm
from real_estate.report.tool.retriever.protocol import Axis, Retriever

logger = logging.getLogger("agent.reviser")


class ReviserOutput(BaseModel):
    body_markdown: str = Field(
        description="수정된 섹션 본문. 기존 [E#] 인용은 가능한 한 유지."
    )
    used_evidence_indexes: list[int] = Field(
        default_factory=list,
        description="추가로 인용한 evidence 인덱스 (기존 + 신규 통합).",
    )


_SYSTEM = korean_system_prompt(
    "리뷰어 코멘트를 충실히 반영해 섹션을 수정하는 reviser. "
    "factual 코멘트는 evidence 재확인 / tone은 어조 조정 / structure는 문단 재구성 / "
    "missing-evidence는 retriever 결과를 더 끌어와 보강."
)


def revise_section(
    *,
    current_body: str,
    axis: Axis,
    section_title: str,
    section_question: str,
    comments: list[dict],
    retriever: Retriever,
) -> tuple[str, list[str], list[str]]:
    """현 본문 + 코멘트 → 수정 본문 + (evidence_node_refs, evidence_snippet_texts).

    comments: [{intent, anchor_start, anchor_end, instruction}, ...]
    """
    snippets = retriever.retrieve(section_question, k=8, axis=axis)
    evidence_text = format_snippets(snippets)

    comment_lines = []
    for i, c in enumerate(comments):
        anchor = ""
        if c.get("anchor_start") is not None and c.get("anchor_end") is not None:
            start = max(0, c["anchor_start"])
            end = min(len(current_body), c["anchor_end"])
            anchor = current_body[start:end][:200]
        comment_lines.append(
            f"코멘트 {i+1} (intent={c.get('intent')}): {c.get('instruction')}\n"
            + (f"  ↳ 지목한 구간: \"{anchor}\"\n" if anchor else "")
        )
    comments_text = "\n".join(comment_lines) or "(코멘트 없음)"

    user = (
        f"섹션 제목: {section_title}\n"
        f"섹션 핵심 질문: {section_question}\n\n"
        f"=== 현재 본문 ===\n{current_body}\n\n"
        f"=== 리뷰어 코멘트 ===\n{comments_text}\n\n"
        f"=== 가용 evidence ===\n{evidence_text}\n\n"
        "위 코멘트를 모두 반영해 본문을 수정하세요. 가능한 한 기존 구조와 "
        "[E#] 인용을 보존하되, missing-evidence 코멘트가 있으면 새 evidence를 "
        "끌어와 [E#] 추가, factual 코멘트는 evidence와 대조해 정정하세요."
    )
    llm = get_llm("reviser").with_structured_output(ReviserOutput)
    result: ReviserOutput = llm.invoke([
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ])

    used_refs: list[str] = []
    used_snippets: list[str] = []
    for idx in result.used_evidence_indexes:
        if 1 <= idx <= len(snippets):
            used_refs.extend(snippets[idx - 1].node_refs)
            used_snippets.append(snippets[idx - 1].text)

    return result.body_markdown, list(dict.fromkeys(used_refs)), used_snippets
