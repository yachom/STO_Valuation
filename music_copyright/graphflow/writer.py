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

from infra.agent.roles.writer import BaseWriterAgent
from music_copyright.graphflow.agents.base import music_system_prompt
from music_copyright.graphflow.composer import format_evidence
from music_copyright.graphflow.prompts import get_prompt

logger = logging.getLogger("music_copyright.writer")

# 기본 축 — influence/popularity/sustainability/social_value (axes.BASE_AXES와 일치).
# report.py는 Axis 객체로 title/focus를 직접 넘기므로 이 표는 키-문자열 호출 시 폴백.
AXIS_TITLES = {
    "influence": "영향력 분석",
    "popularity": "대중성·팬덤 분석",
    "sustainability": "지속성 분석",
    "social_value": "사회적 가치 분석",
}

AXIS_FOCUS = {
    "influence": "비평·문화적 영향, 수상·미디어 언급, 창작자(작곡·작사) 위상, 샘플·커버·인용",
    "popularity": "차트 성적, 스트리밍·플랫폼 도달, 청취자 규모, 팬덤 활동",
    "sustainability": "롱테일 스트리밍·재생 지속, 권리 카탈로그 안정성, 라이선스 다양성, 세대 전이",
    "social_value": "작품이 다루는 사회적 의제와 메시지, 사회운동·공익 연계, 대표성·다양성, "
                    "아티스트/가수와 팬덤이 만든 공익·문화외교 맥락, 공동체·세대·국가를 "
                    "잇는 사회적 파급과 공적 활용. 단, 작품 자체 근거와 아티스트/팬덤 "
                    "맥락은 구분",
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


class MusicWriter(BaseWriterAgent):
    """음악 섹션 집필 — BaseWriterAgent 골격 + 음악 도메인 hook.

    write()는 베이스가 제공한다. 여기선 음악 프롬프트 슬롯·스키마·프레임·축 표만 공급.
    title/focus를 명시로 받는 동적 축이 있으므로 strict_axis=False(미등록 축은 폴백).
    """

    name = "music_writer"
    Schema = SectionDraft
    role_key = "music_writer.role"
    rules_key = "music_writer.rules"
    AXIS_TITLES = AXIS_TITLES
    AXIS_FOCUS = AXIS_FOCUS
    card_label = "[평가 대상 저작물]"
    outline_intro = "[아웃라인 — 이 논점들을 순서대로, 단 증거가 뒷받침하는 범위에서만]"
    strict_axis = False

    def _get_prompt(self, key, **fmt):
        return get_prompt(key, **fmt)

    def _format_evidence(self, evidence):
        return format_evidence(evidence)

    def _frame(self, role):
        return music_system_prompt(role)
