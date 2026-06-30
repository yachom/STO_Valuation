"""Writer — 축별 섹션 집필, 문장-인용 쌍 강제 (설계 §10.1, D12).

structured output으로 blocks(문단)+claims(주장)를 분리해 받는다:
    - factual claim → cites([E#]) 필수. 그래프 인용 없는 사실 주장은 Auditor
      기계층이 차단.
    - opinion claim → 같은 섹션의 factual claim에서 도출됐음을 명시(derived_from).
E# → 노드 ref 매핑은 코드가 한다 (LLM은 인덱스만 다룸).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from infra.agent.roles.writer import BaseWriterAgent
from real_estate.graphflow.agents.base import korean_system_prompt
from real_estate.graphflow.prompts import get_prompt
from real_estate.graphflow.composer import format_evidence


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


class Computation(BaseModel):
    """파생수치 산식 — 본문 숫자가 인용 증거 값들의 계산 결과일 때 첨부.

    LLM이 머릿속 산수로 낸 숫자는 신뢰하지 않는다(환각 위험). 산식과 결과를 같이
    내면 Auditor가 코드로 재계산해 검증한다(a-2): ① expression의 입력 숫자가 인용
    증거에 실재하는지 ② expression을 평가한 값이 result와 일치하는지. 둘 다 통과해야
    본문의 파생수치가 인정된다.
    """
    expression: str = Field(
        description="산식 — 인용 증거에 있는 원 숫자와 + - * / ( ) 만 사용. 예: '829924 / 33880'")
    result: float = Field(description="이 산식을 계산한 결과값 (본문에 쓰는 파생수치의 출처)")


class ClaimDraft(BaseModel):
    text: str = Field(description="주장 한 문장 (한국어)")
    kind: Literal["factual", "opinion", "gap"] = Field(
        description="factual=증거에 직접 근거한 사실 / opinion=사실들에서 도출한 해석·판단 / "
                    "gap=증거가 다루지 않는 정보의 부재·유보 진술('~자료는 증거에 없다/부족하다', "
                    "'확인되지 않아 유보'). gap은 cites·derived_from가 불요하나 수치를 담을 수 없다")
    cites: list[int] = Field(
        default_factory=list,
        description="근거 evidence 번호들 (E3이면 3). factual은 1개 이상 필수 (gap·opinion은 불요)")
    derived_from: list[int] = Field(
        default_factory=list,
        description="opinion일 때 — 이 출력의 claims 배열에서 근거가 되는 claim 인덱스(0-base)")
    computations: list[Computation] = Field(
        default_factory=list,
        description="본문에 비율·배수·백분율·단위환산·반올림 등 '계산한' 수치가 있으면 각각 산식 첨부. "
                    "증거에 그대로 있는 값을 직접 인용한 경우엔 불요")
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


class Writer(BaseWriterAgent):
    """상가 섹션 집필 — BaseWriterAgent 골격 + 부동산 도메인 hook.

    write()는 베이스가 제공한다. card_label·outline_intro·strict_axis는 베이스
    기본값(부동산형)을 그대로 쓰고, 프롬프트 슬롯·스키마·프레임·축 표만 공급한다.
    """

    name = "writer"
    Schema = SectionDraft
    role_key = "writer.role"
    rules_key = "writer.rules"
    AXIS_TITLES = AXIS_TITLES
    AXIS_FOCUS = AXIS_FOCUS

    def _get_prompt(self, key, **fmt):
        return get_prompt(key, **fmt)

    def _format_evidence(self, evidence):
        return format_evidence(evidence)

    def _frame(self, role):
        return korean_system_prompt(role)
