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
    markdown: str = Field(description="문단 markdown (3~6문장)")
    claim_indexes: list[int] = Field(
        default_factory=list,
        description="이 문단이 담는 claim들의 인덱스 (claims 배열 0-base)")


class SectionDraft(BaseModel):
    title: str = Field(description="섹션 제목")
    blocks: list[BlockDraft] = Field(description="문단 3~5개")
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
            f"'{AXIS_TITLES[axis]}' 섹션 집필자. 초점: {AXIS_FOCUS[axis]}"
        ) + (
            "\n출력 규칙:\n"
            "- 모든 주장을 claims 배열에 문장 단위로 분리합니다.\n"
            "- factual 주장은 반드시 cites에 근거 [E#] 번호를 답니다. 인용 없는 "
            "사실 주장은 시스템이 거부합니다.\n"
            "- 숫자·날짜는 인용한 evidence에 있는 값 그대로만 씁니다.\n"
            "- factual 주장에는 as_of(기준 시점)를 답니다 — 인용 evidence의 시점.\n"
            "- 오래된 증거(시점이 수년 전이거나 유효성=DECAYED/EXPIRED)를 쓸 때는 "
            "본문에 시점을 명시하고 현재와 다를 수 있음을 표시합니다.\n"
            "- opinion 주장은 derived_from으로 근거 claim을 명시합니다.\n"
            "- 구조 통계(준공·인증·분기 매출 등)는 '현황'을, 뉴스 evidence는 '전망·"
            "개발 호재·입주 기업·시장 변화' 같은 동적 맥락을 담습니다 — 둘은 보완 관계. "
            "자산 가치에 영향 주는 개발 호재·배후수요 변화·시장 동향이 뉴스 evidence에 "
            "있으면 적극 인용해 정적 통계를 보완하세요(통계와 중복되지 않는 미래·맥락 정보).\n"
            "- blocks의 markdown 문단과 claims는 내용이 일치해야 합니다."
        )
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
