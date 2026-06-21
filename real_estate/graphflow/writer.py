"""Writer — 축별 섹션 집필, 문장-인용 쌍 강제 (설계 §10.1, D12).

structured output으로 blocks(문단)+claims(주장)를 분리해 받는다:
    - factual claim → cites([E#]) 필수. 그래프 인용 없는 사실 주장은 Auditor
      기계층이 차단.
    - opinion claim → 같은 섹션의 factual claim에서 도출됐음을 명시(derived_from).
E# → 노드 ref 매핑은 코드가 한다 (LLM은 인덱스만 다룸).
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from real_estate.graphflow.agents.base import BaseAgent, korean_system_prompt
from real_estate.graphflow.prompts import get_prompt
from real_estate.graphflow.composer import Evidence, format_evidence

logger = logging.getLogger("graphflow.writer")

AXIS_TITLES = {
    "tenant": "임차(테넌트) 가치 분석",
    "esg": "ESG·물리적 상태 분석",
    "commercial": "입지·상권 가치 분석",
}

AXIS_FOCUS = {
    "tenant": "임차 사업자 안정성 — 임대 시장(공실/임대료/수익률), 업종 회전율, 임차 수요의 질",
    "esg": "건물의 에너지 효율·녹색 인증 등 ESG 측면과 물리적 상태(준공·규모), 자치구 내 상대 위치",
    "commercial": "상권 건강도(유동인구/매출 추이/주력 업종), 배후수요, 교통 접근성, 개발 호재",
}


class ClaimDraft(BaseModel):
    text: str = Field(description="주장 한 문장 (한국어)")
    kind: Literal["factual", "opinion"] = Field(
        description="factual=증거에 직접 근거한 사실 / opinion=사실들에서 도출한 해석·판단")
    cites: list[int] = Field(
        default_factory=list,
        description="근거 evidence 번호들 (E3이면 3). factual은 1개 이상 필수")
    derived_from: list[int] = Field(
        default_factory=list,
        description="opinion일 때 — 이 출력의 claims 배열에서 근거가 되는 claim 인덱스(0-base)")
    as_of: str | None = Field(
        default=None, description="주장의 기준 시점 (인용 증거의 보도/기준 시점, 예: 2025-04)")


class BlockDraft(BaseModel):
    markdown: str = Field(description="문단 markdown (4~8문장 — 논점을 충분히 전개, 깊이 우선)")
    claim_indexes: list[int] = Field(
        default_factory=list,
        description="이 문단이 담는 claim들의 인덱스 (claims 배열 0-base)")


class SectionDraft(BaseModel):
    title: str = Field(description="섹션 제목")
    blocks: list[BlockDraft] = Field(
        description="문단 4~7개 (다룰 논점이 많으면 더 — 깊이·완결성 우선)")
    claims: list[ClaimDraft] = Field(description="이 섹션의 모든 주장 (문장 단위)")


class Writer(BaseAgent):
    name = "writer"

    def write(
        self,
        axis: str,
        asset_card: str,
        evidence: list[Evidence],
        *,
        violations: list[str] | None = None,
    ) -> SectionDraft:
        """섹션 1개 집필. violations가 있으면 재작성 모드 (위반 목록 첨부)."""
        system = korean_system_prompt(
            get_prompt("writer.role", title=AXIS_TITLES[axis], focus=AXIS_FOCUS[axis])
        ) + get_prompt("writer.rules")
        user_parts = [
            f"[평가 대상 자산]\n{asset_card}",
            f"[증거 — 그래프 walk 수집, 경로 포함]\n{format_evidence(evidence)}",
            "위 증거만으로 섹션을 작성하세요. 증거가 부족한 논점은 부족하다고 명시하세요.",
        ]
        if violations:
            user_parts.append(
                "[재작성 — 직전 출력의 위반 사항을 반드시 수정]\n"
                + "\n".join(f"- {v}" for v in violations))
        out: SectionDraft = self.complete(
            system=system, user="\n\n".join(user_parts), schema=SectionDraft)
        logger.info("writer(%s): blocks=%d claims=%d%s", axis,
                    len(out.blocks), len(out.claims),
                    " (재작성)" if violations else "")
        return out
