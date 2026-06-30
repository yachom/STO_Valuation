"""Synthesizer (음악저작권) — 3축 섹션 → Overview (요약 + 정성 가치 등급). real_estate 미러.

가치 결론은 정성 등급(A+~D)만 — 정량 금액 산정 없음. Overview의 factual 주장은
섹션 claim([C#])을 인용한다. LLM은 music_synthesizer 티어(GPT).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from infra.agent.roles.synthesizer import BaseSynthesizerAgent
from music_copyright.graphflow.agents.base import music_system_prompt
from music_copyright.graphflow.prompts import get_prompt


Grade = Literal["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D"]
OptionalGrade = Literal["", "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D"]


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
    grade: Grade = Field(description="사회·문화 가치 등급 (A+~D). 정량 금액 아님")
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
    # ── 이중 트랙: 자산가치(상업)와 사회적 가치를 분리 판정 — 한쪽이 다른 쪽을 가리지 않게 ──
    asset_grade: OptionalGrade = Field(
        default="", description="자산가치(상업) 등급 (A+~D). 영향력·대중성·지속성 축 종합")
    asset_rationale: str = Field(
        default="", description="자산가치 등급 논거 — 상업적 강점·약점·한계")
    social_grade: OptionalGrade = Field(
        default="", description="사회적 가치 등급 (A+~D). 사회적 가치 축(및 사회성 띤 추가 "
                                "축) 종합 — 상업 성과로 대신 매기지 말 것")
    social_rationale: str = Field(
        default="", description="사회적 가치 등급 논거 — 사회적 의제·공익 연계·대표성·공동체/"
                                "소프트파워 파급. 사회적 근거가 부족하면 보수적으로 + 한계 명시")
    overall_grade: OptionalGrade = Field(
        default="", description="종합 등급 (A+~D). 자산가치와 사회적 가치를 함께 반영한 단일 판정")
    overall_rationale: str = Field(
        default="", description="종합 등급의 핵심 논거 — 두 트랙의 결정적 강점·약점·한계")
    axis_scores: list[AxisScore] = Field(
        default_factory=list,
        description="제시된 *모든* 평가 축마다 등급+근거 1개씩 (기본 4축 + 추가 축 전부). "
                    "섹션 수만큼 — 누락 금지")


class MusicSynthesizer(BaseSynthesizerAgent):
    """음악 종합 — BaseSynthesizerAgent 골격 + 음악 도메인 hook.

    synthesize()와 format_sections는 베이스가 제공한다. 여기선 이중 트랙 OverviewDraft
    스키마·음악 프롬프트 슬롯·프레임·라벨, 그리고 [채점할 축] 목록 추가만 공급한다.
    """

    name = "music_synthesizer"
    Schema = OverviewDraft
    role_key = "music_synthesizer.role"
    rules_key = "music_synthesizer.rules"
    card_label = "[평가 대상 저작물]"
    sections_label = "[평가 축 섹션과 주장 목록]"

    def _get_prompt(self, key, **fmt):
        return get_prompt(key, **fmt)

    def _frame(self, role):
        return music_system_prompt(role)

    def _build_user(self, card, sections_text, sections):
        axis_titles = [sec.title for sec in sections.values()]
        return (super()._build_user(card, sections_text, sections)
                + "\n\n[채점할 축 — 각 축마다 axis_scores 1개씩, 누락 금지]\n"
                + "\n".join(f"- {t}" for t in axis_titles))
