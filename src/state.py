"""LangGraph state schema."""

from __future__ import annotations

from typing import Any, TypedDict


class AxisResult(TypedDict, total=False):
    axis: str
    score: float  # 0~100
    grade: str  # A+, A, B, C, D
    strengths: list[str]
    weaknesses: list[str]
    risks: list[str]
    evidence: dict[str, Any]
    rationale: str


class PipelineState(TypedDict, total=False):
    address: str
    metadata: dict[str, Any]
    esg: AxisResult
    awareness: AxisResult
    vitality: AxisResult
    report_markdown: str
    errors: list[str]
