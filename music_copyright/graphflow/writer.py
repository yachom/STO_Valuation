"""Writer (음악저작권) — 축별 섹션 집필, 문장-인용 쌍 강제. real_estate 미러.

structured output으로 blocks(문단)+claims(주장)를 분리해 받는다:
    - factual claim → cites([E#]) 필수. 인용 없는 사실 주장은 Auditor 기계층이 차단.
    - opinion claim → 같은 섹션 factual claim에서 도출됐음을 명시(derived_from).
E# → 노드 ref 매핑은 코드가 한다 (LLM은 인덱스만 다룸). LLM은 music_writer 티어(GPT).
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import BaseAgent, music_system_prompt
from music_copyright.graphflow.composer import Evidence, format_evidence
from music_copyright.graphflow.prompts import get_prompt

logger = logging.getLogger("music_copyright.writer")

# 3축 — influence/popularity/sustainability (profiles.REPORT_AXES와 일치)
AXIS_TITLES = {
    "influence": "영향력 분석",
    "popularity": "대중성·팬덤 분석",
    "sustainability": "지속성 분석",
}

AXIS_FOCUS = {
    "influence": "비평·문화적 영향, 수상·미디어 언급, 창작자(작곡·작사) 위상, 샘플·커버·인용",
    "popularity": "차트 성적, 스트리밍·플랫폼 도달, 청취자 규모, 팬덤 활동",
    "sustainability": "롱테일 스트리밍·재생 지속, 권리 카탈로그 안정성, 라이선스 다양성, 세대 전이",
}


class Computation(BaseModel):
    """파생수치 산식 — 본문 숫자가 인용 증거 값들의 계산 결과일 때 첨부."""
    expression: str = Field(
        description="산식 — 인용 증거에 있는 원 숫자와 + - * / ( ) 만 사용. 예: '176040111 / 1104330'")
    result: float = Field(description="이 산식을 계산한 결과값 (본문에 쓰는 파생수치의 출처)")


class ClaimDraft(BaseModel):
    text: str = Field(description="주장 한 문장 (한국어)")
    kind: Literal["factual", "opinion", "gap"] = Field(
        description="factual=증거에 직접 근거한 사실 / opinion=사실들에서 도출한 해석·판단 / "
                    "gap=증거가 다루지 않는 정보의 부재·유보 진술. gap은 cites·derived_from 불요, 수치 불가")
    cites: list[int] = Field(
        default_factory=list,
        description="근거 evidence 번호들 (E3이면 3). factual은 1개 이상 필수 (gap·opinion은 불요)")
    derived_from: list[int] = Field(
        default_factory=list,
        description="opinion일 때 — 이 출력의 claims 배열에서 근거가 되는 claim 인덱스(0-base)")
    computations: list[Computation] = Field(
        default_factory=list,
        description="본문에 비율·배수·백분율 등 '계산한' 수치가 있으면 각각 산식 첨부. 직접 인용은 불요")
    as_of: str | None = Field(
        default=None, description="주장의 기준 시점 (인용 증거의 보도/기준 시점)")


class BlockDraft(BaseModel):
    markdown: str = Field(description="문단 markdown (4~8문장 — 논점을 충분히 전개, 깊이 우선)")
    claim_indexes: list[int] = Field(
        default_factory=list, description="이 문단이 담는 claim들의 인덱스 (claims 배열 0-base)")


class SectionDraft(BaseModel):
    title: str = Field(description="섹션 제목")
    blocks: list[BlockDraft] = Field(description="문단 3~6개 (깊이·완결성 우선)")
    claims: list[ClaimDraft] = Field(description="이 섹션의 모든 주장 (문장 단위)")


class MusicWriter(BaseAgent):
    name = "music_writer"

    def write(
        self,
        axis: str,
        work_card: str,
        evidence: list[Evidence],
        *,
        violations: list[str] | None = None,
        outline: list[str] | None = None,
        title: str | None = None,
        focus: str | None = None,
    ) -> SectionDraft:
        """섹션 1개 집필. violations면 재작성 모드. outline 있으면 그 순서로 구성.

        title/focus 미지정이면 기본 3축 룩업, 지정되면 그대로(동적 축 — 라벨·의도 전달).
        """
        title = title or AXIS_TITLES.get(axis, f"{axis} 분석")
        focus = focus or AXIS_FOCUS.get(axis, "")
        system = music_system_prompt(
            get_prompt("music_writer.role", title=title, focus=focus)
        ) + get_prompt("music_writer.rules")
        user_parts = [
            f"[평가 대상 저작물]\n{work_card}",
            f"[증거 — 그래프 walk 수집, 경로 포함]\n{format_evidence(evidence)}",
            "위 증거만으로 섹션을 작성하세요. 증거가 부족한 논점은 부족하다고 명시하세요.",
        ]
        if outline:
            user_parts.append(
                "[아웃라인 — 이 논점들을 순서대로, 단 증거가 뒷받침하는 범위에서만]\n"
                + "\n".join(f"- {p}" for p in outline))
        if violations:
            user_parts.append(
                "[재작성 — 직전 출력의 위반 사항을 반드시 수정]\n"
                + "\n".join(f"- {v}" for v in violations))
        out: SectionDraft = self.complete(
            system=system, user="\n\n".join(user_parts), schema=SectionDraft)
        logger.info("music_writer(%s): blocks=%d claims=%d%s", axis,
                    len(out.blocks), len(out.claims), " (재작성)" if violations else "")
        return out
