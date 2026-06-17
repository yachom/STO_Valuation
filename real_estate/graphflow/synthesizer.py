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


class AxisScore(BaseModel):
    axis: str = Field(description="평가 축 (예: 상권건강도 / ESG·물리성능 / 배후수요 / 임차안정성)")
    grade: str = Field(description="사회적 가치 등급 (A+/A/A-/B+/B/B-/C+/C/C-/D). "
                                   "감정평가액 같은 정량 금액이 아니라 정성 등급")
    rationale: str = Field(description="등급 근거 — 가능하면 벤치마크(자치구 순위·상위%·추세)를 "
                                       "인용. 근거 데이터가 부재한 축은 그 한계를 명시하고 등급을 보수적으로")


class OverviewDraft(BaseModel):
    title: str = Field(description="섹션 제목 (예: '종합 평가 개요')")
    blocks: list["OverviewBlockDraft"] = Field(description="문단 2~4개")
    claims: list[OverviewClaimDraft]
    overall_grade: str = Field(
        default="",
        description="사회적 가치 종합 등급 (A+~D). 축 점수를 가중 종합한 단일 판정")
    overall_rationale: str = Field(
        default="", description="종합 등급의 핵심 논거 — 결정적 강점·약점·한계")
    axis_scores: list[AxisScore] = Field(
        default_factory=list, description="축별 등급+근거 (상권건강도·ESG·배후수요·임차안정성)")


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
            "종합 평가 작성자(Synthesizer). 3축 섹션을 통합해 Executive Summary, "
            "축간 인사이트, 그리고 사회적 가치 **등급 판정(루브릭)**을 작성합니다. "
            "감정평가액 같은 정량 금액은 만들지 마세요 — 대신 정성 **등급**(A+~D)으로 "
            "사회적 가치를 판정합니다."
        ) + (
            "\n출력 규칙:\n"
            "- factual 주장은 반드시 cites에 근거 섹션 주장 [C#] 번호를 답니다.\n"
            "- 섹션에 없는 새로운 사실을 만들지 마세요.\n"
            "- 축간 모순이 보이면 숨기지 말고 명시적으로 다루세요.\n"
            "\n[루브릭 — 핵심]\n"
            "- axis_scores: 상권건강도·ESG·물리성능·배후수요·임차안정성 등 축마다 "
            "grade(A+~D)와 rationale을 매깁니다.\n"
            "- 등급 근거는 **비교 사실**을 우선 인용하세요 — 섹션에 '자치구 N곳 중 K위', "
            "'상위 X%', '추세 ±Y%', '최고 등급' 같은 벤치마크가 있으면 그것이 등급의 핵심 근거입니다.\n"
            "- **데이터가 부재한 축(예: 임차 공실률·임대료·회전율 미수집)은 등급을 보수적으로 "
            "매기고 rationale에 '핵심 정량 데이터 부재로 잠정 평가'임을 명시**하세요. 없는 걸 "
            "있는 척 점수화하지 마세요.\n"
            "- overall_grade: 축 등급을 종합한 단일 판정 + overall_rationale에 결정적 근거.\n"
            "- 마지막 block 문단은 이 등급 판정을 산문으로 요약(강점/약점/유의점)."
        )
        user = (f"[평가 대상 자산]\n{asset_card}\n\n"
                f"[3축 섹션과 주장 목록]\n{sections_text}")
        out: OverviewDraft = self.complete(system=system, user=user, schema=OverviewDraft)
        logger.info("synthesizer: blocks=%d claims=%d", len(out.blocks), len(out.claims))
        return out, index_map
