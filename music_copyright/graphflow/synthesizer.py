"""Synthesizer (음악저작권) — 3축 섹션 → Overview (요약 + 정성 가치 등급). real_estate 미러.

가치 결론은 정성 등급(A+~D)만 — 정량 금액 산정 없음. Overview의 factual 주장은
섹션 claim([C#])을 인용한다. LLM은 music_synthesizer 티어(GPT).
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import BaseAgent, music_system_prompt
from music_copyright.graphflow.prompts import get_prompt
from music_copyright.graphflow.writer import SectionDraft

logger = logging.getLogger("music_copyright.synthesizer")


class OverviewClaimDraft(BaseModel):
    text: str = Field(description="주장 한 문장")
    kind: Literal["factual", "opinion"] = Field(
        description="factual=섹션 claim에 직접 근거 / opinion=종합 해석")
    cites: list[int] = Field(
        default_factory=list,
        description="근거 섹션 claim 번호들 (C7이면 7). factual은 1개 이상 필수")
    as_of: str | None = None


class AxisScore(BaseModel):
    axis: str = Field(description="평가 축 이름 — 아래 제시된 섹션의 축 라벨 그대로(기본 "
                                  "영향력/대중성/지속성 + 추가 축 포함)")
    grade: str = Field(description="사회·문화 가치 등급 (A+/A/A-/B+/B/B-/C+/C/C-/D). 정량 금액 아님")
    rationale: str = Field(
        description="등급 근거. 데이터가 부재한 축은 한계를 명시하고 등급을 보수적으로")


class OverviewBlockDraft(BaseModel):
    markdown: str
    claim_indexes: list[int] = Field(
        default_factory=list, description="claims 배열 0-base 인덱스")


class OverviewDraft(BaseModel):
    title: str = Field(description="섹션 제목 (예: '종합 평가 개요')")
    blocks: list[OverviewBlockDraft] = Field(description="문단 2~4개")
    claims: list[OverviewClaimDraft]
    overall_grade: str = Field(
        default="", description="사회·문화 가치 종합 등급 (A+~D). 축 점수를 종합한 단일 판정")
    overall_rationale: str = Field(
        default="", description="종합 등급의 핵심 논거 — 결정적 강점·약점·한계")
    axis_scores: list[AxisScore] = Field(
        default_factory=list,
        description="제시된 *모든* 평가 축마다 등급+근거 1개씩 (기본 3축 + 추가 축 전부). "
                    "섹션 수만큼 — 누락 금지")


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


class MusicSynthesizer(BaseAgent):
    name = "music_synthesizer"

    def synthesize(
        self,
        work_card: str,
        sections: dict[str, SectionDraft],
    ) -> tuple[OverviewDraft, list[tuple[str, int]]]:
        """Overview 생성. 반환: (draft, C# 글로벌 인덱스 → (axis, claim_idx) 매핑)."""
        sections_text, index_map = _format_sections(sections)
        axis_titles = [sec.title for sec in sections.values()]
        system = (music_system_prompt(get_prompt("music_synthesizer.role"))
                  + get_prompt("music_synthesizer.rules"))
        user = (f"[평가 대상 저작물]\n{work_card}\n\n"
                f"[평가 축 섹션과 주장 목록]\n{sections_text}\n\n"
                f"[채점할 축 — 각 축마다 axis_scores 1개씩, 누락 금지]\n"
                + "\n".join(f"- {t}" for t in axis_titles))
        out: OverviewDraft = self.complete(system=system, user=user, schema=OverviewDraft)
        logger.info("music_synthesizer: blocks=%d claims=%d grade=%s",
                    len(out.blocks), len(out.claims), out.overall_grade)
        return out, index_map
