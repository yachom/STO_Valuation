"""Evaluation claims + analysis result for commercial asset."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from projects.commercial.domain.factors import FactorAxis
from projects.commercial.domain.signals import Evidence


class EvaluationClaim(BaseModel):
    factor_key: str  # COMMERCIAL_EVALUATION_FACTORS key
    axis: FactorAxis
    score: float = 0.0  # 0.0 = stub for quantitative/structural in current scope
    rationale: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    target_id: str  # PNU
    address: str | None = None
    claims: list[EvaluationClaim] = Field(default_factory=list)
    evidence_trace: list[Evidence] = Field(default_factory=list)
    report_text: str = ""
    scope_id: str
    created_at: datetime
