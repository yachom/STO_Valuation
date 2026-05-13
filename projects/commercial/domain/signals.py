"""Commercial-asset market signals + evidence."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from projects.commercial.domain.ontology import OntologyTag


class SignalType(str, Enum):
    MARKET = "market"
    POLICY = "policy"
    TENANT = "tenant"
    LOCAL = "local"
    MACRO = "macro"


class Evidence(BaseModel):
    source: str  # e.g. "naver_news", "vworld", "molit"
    url: str | None = None
    snippet: str | None = None
    captured_at: datetime
    raw_meta: dict[str, Any] = Field(default_factory=dict)


class MarketSignal(BaseModel):
    signal_id: str
    signal_type: SignalType
    tag: OntologyTag
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)
    created_at: datetime
