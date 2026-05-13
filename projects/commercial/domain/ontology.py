"""Commercial-asset ontology — 6-axis tagging vocabulary.

These axes are the *current best guess* for commercial real estate. They may
not transfer 1:1 to other assets (e.g. music); when a second asset arrives,
re-evaluate whether to lift any axis into ``common/domain``.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SocietyAxis(str, Enum):
    NONE = "none"
    SOCIAL_TREND = "social_trend"
    DEMOGRAPHIC = "demographic"


class PolicyAxis(str, Enum):
    NONE = "none"
    REGULATION = "regulation"
    URBAN_PLAN = "urban_plan"
    TAX = "tax"


class TenantAxis(str, Enum):
    NONE = "none"
    INDUSTRY_BOOM = "industry_boom"
    INDUSTRY_BUST = "industry_bust"


class MarketAxis(str, Enum):
    NONE = "none"
    PRICE_UP = "price_up"
    PRICE_DOWN = "price_down"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"


class MacroAxis(str, Enum):
    NONE = "none"
    RATE_UP = "rate_up"
    RATE_DOWN = "rate_down"
    INFLATION = "inflation"


class LocalAxis(str, Enum):
    NONE = "none"
    REDEVELOPMENT = "redevelopment"
    INFRA_OPEN = "infra_open"
    INFRA_CLOSE = "infra_close"


class OntologyTag(BaseModel):
    society: SocietyAxis = SocietyAxis.NONE
    policy: PolicyAxis = PolicyAxis.NONE
    tenant: TenantAxis = TenantAxis.NONE
    market: MarketAxis = MarketAxis.NONE
    macro: MacroAxis = MacroAxis.NONE
    local: LocalAxis = LocalAxis.NONE
    factors: list[str] = Field(
        default_factory=list,
        description="Keys from COMMERCIAL_EVALUATION_FACTORS this signal affects.",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
