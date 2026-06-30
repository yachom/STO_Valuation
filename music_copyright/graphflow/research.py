"""Structured research contracts shared by planning, search, and feedback."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


ResearchAxis = Literal[
    "influence", "popularity", "sustainability", "social_value", "cross_axis",
]
EvidenceType = Literal[
    "direct_work", "artist_context", "metric", "critical_reception",
    "rights_context", "public_use", "verification", "discovery",
]
SearchChannel = Literal["web", "news", "youtube"]
SearchStrategy = Literal[
    "exact", "official", "metric", "criticism", "event", "context",
    "verification", "discovery",
]


class QueryVariant(BaseModel):
    """One channel-aware search expression for a research task."""

    text: str = Field(description="검색 엔진에 전달할 실제 검색어")
    channels: list[SearchChannel] = Field(
        default_factory=lambda: ["web", "news"],
        description="이 검색어를 실행할 채널",
    )
    strategy: SearchStrategy = Field(
        default="discovery", description="질의 변형의 조사 전략")
    expected_signal: str = Field(
        default="", description="이 검색어에서 기대하는 검증 가능한 신호")


class ResearchTask(BaseModel):
    """An evidence need plus executable query variants.

    ``queries`` is retained as a compatibility input for older callers. New
    planners should populate ``variants`` so channel and strategy intent are
    not lost before execution.
    """

    task_id: str = Field(default="", description="검증 단계에서 부여하는 안정적 task id")
    target: str = Field(description="후보 목록의 ref, 예: Work:isrc:...")
    axis: ResearchAxis = Field(default="cross_axis")
    question: str = Field(default="", description="웹 조사로 답할 구체적인 질문")
    evidence_goal: str = Field(default="", description="필요한 증거와 충분 조건")
    evidence_type: EvidenceType = Field(default="discovery")
    source_preferences: list[str] = Field(
        default_factory=list,
        description="공식 DB, 차트 운영사, 원문 인터뷰 등 우선 출처 유형",
    )
    variants: list[QueryVariant] = Field(default_factory=list)
    queries: list[str] = Field(
        default_factory=list,
        description="레거시 검색어 입력. variants가 없을 때만 사용",
    )
    why: str = Field(default="", description="이 과제를 지금 수행해야 하는 이유")

    def query_variants(self) -> list[QueryVariant]:
        if self.variants:
            return list(self.variants)
        return [QueryVariant(text=q) for q in self.queries if q.strip()]


@dataclass
class QueryOutcome:
    """Per-query execution result used by the next planning round."""

    task_id: str
    target: str
    axis: str
    query: str
    strategy: str
    channels: list[str]
    docs_selected: int = 0
    docs_ingested: int = 0
    docs_new: int = 0
    grounded_facts: int = 0
    facts_new: int = 0
    entities_new: int = 0
    mentions_confirmed: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    sources: set[str] = field(default_factory=set)

    @property
    def utility(self) -> float:
        if self.docs_selected <= 0:
            return 0.0
        precision = self.docs_ingested / self.docs_selected
        fact_yield = min(1.0, self.grounded_facts / max(1, self.docs_selected * 2))
        diversity = min(1.0, len(self.sources) / 3)
        return round(0.5 * precision + 0.35 * fact_yield + 0.15 * diversity, 3)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "target": self.target,
            "axis": self.axis,
            "query": self.query,
            "strategy": self.strategy,
            "channels": self.channels,
            "docs_selected": self.docs_selected,
            "docs_ingested": self.docs_ingested,
            "docs_new": self.docs_new,
            "grounded_facts": self.grounded_facts,
            "facts_new": self.facts_new,
            "entities_new": self.entities_new,
            "mentions_confirmed": self.mentions_confirmed,
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
            "sources": sorted(self.sources),
            "utility": self.utility,
        }
