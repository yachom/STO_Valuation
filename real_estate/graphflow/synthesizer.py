"""Synthesizer — 3축 섹션 → Overview (요약 + 축간 인사이트 + 정성 종합의견).

가치 결론은 정성 서술만 (D1 — 정량 산정 없음). Overview의 factual 주장은
섹션 claim([C#])을 인용한다 — Claim grounding이 Overview에도 동일 적용.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from real_estate.graphflow.agents.base import BaseAgent, korean_system_prompt
from real_estate.graphflow.writer import SectionDraft

logger = logging.getLogger("graphflow.synthesizer")


class OverviewClaimDraft(BaseModel):
    text: str = Field(description="주장 한 문장")
    kind: Literal["factual", "opinion"] = Field(
        description="factual=섹션 claim에 직접 근거 / opinion=종합 해석")
    cites: list[int] = Field(
        default_factory=list,
        description="근거 섹션 claim 번호들 (C7이면 7). factual은 1개 이상 필수")
    as_of: str | None = None


class OverviewDraft(BaseModel):
    title: str = Field(description="섹션 제목 (예: '종합 평가 개요')")
    blocks: list["OverviewBlockDraft"] = Field(description="문단 2~4개")
    claims: list[OverviewClaimDraft]


class OverviewBlockDraft(BaseModel):
    markdown: str
    claim_indexes: list[int] = Field(
        default_factory=list, description="claims 배열 0-base 인덱스")


def _format_sections(sections: dict[str, SectionDraft]) -> tuple[str, list[tuple[str, int]]]:
    """섹션들을 [C#] 글로벌 인덱스 텍스트로 직렬화. 반환: (텍스트, C#→(axis, claim_idx))."""
    lines: list[str] = []
    index_map: list[tuple[str, int]] = []
    c = 0
    for axis, sec in sections.items():
        lines.append(f"\n## [{axis}] {sec.title}")
        for b in sec.blocks:
            lines.append(b.markdown)
        lines.append("주장 목록:")
        for i, cl in enumerate(sec.claims):
            c += 1
            index_map.append((axis, i))
            as_of = f" (기준 {cl.as_of})" if cl.as_of else ""
            lines.append(f"  [C{c}] ({cl.kind}) {cl.text}{as_of}")
    return "\n".join(lines), index_map


class Synthesizer(BaseAgent):
    name = "synthesizer"

    def synthesize(
        self,
        asset_card: str,
        sections: dict[str, SectionDraft],
    ) -> tuple[OverviewDraft, list[tuple[str, int]]]:
        """Overview 생성. 반환: (draft, C# 글로벌 인덱스 → (axis, claim_idx) 매핑)."""
        sections_text, index_map = _format_sections(sections)
        system = korean_system_prompt(
            "종합 평가 작성자(Synthesizer). 3축 섹션을 통합해 Executive Summary와 "
            "축간 인사이트, 정성적 종합의견을 작성합니다. 가치는 정성 서술만 — "
            "감정평가액 같은 정량 수치를 만들지 마세요."
        ) + (
            "\n출력 규칙:\n"
            "- factual 주장은 반드시 cites에 근거 섹션 주장 [C#] 번호를 답니다.\n"
            "- 섹션에 없는 새로운 사실을 만들지 마세요.\n"
            "- 축간 모순이 보이면 숨기지 말고 명시적으로 다루세요.\n"
            "- 마지막 문단은 자산의 강점/약점/유의점을 균형 있게 담은 종합의견."
        )
        user = (f"[평가 대상 자산]\n{asset_card}\n\n"
                f"[3축 섹션과 주장 목록]\n{sections_text}")
        out: OverviewDraft = self.complete(system=system, user=user, schema=OverviewDraft)
        logger.info("synthesizer: blocks=%d claims=%d", len(out.blocks), len(out.claims))
        return out, index_map
