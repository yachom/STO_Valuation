"""Commercial real-estate evaluation factors.

Keep the dictionary open-ended — adding a factor is a one-line change.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class FactorAxis(str, Enum):
    QUANTITATIVE = "quantitative"
    STRUCTURAL = "structural"
    SENTIMENT = "sentiment"


class CommercialFactor(BaseModel):
    key: str
    name_ko: str
    axis: FactorAxis
    description: str = ""

    model_config = {"frozen": True}


COMMERCIAL_EVALUATION_FACTORS: dict[str, CommercialFactor] = {
    "rental_yield": CommercialFactor(
        key="rental_yield", name_ko="임대수익률", axis=FactorAxis.QUANTITATIVE
    ),
    "vacancy_rate": CommercialFactor(
        key="vacancy_rate", name_ko="공실률", axis=FactorAxis.QUANTITATIVE
    ),
    "transaction_volume": CommercialFactor(
        key="transaction_volume", name_ko="거래량", axis=FactorAxis.QUANTITATIVE
    ),
    "commercial_district_vitality": CommercialFactor(
        key="commercial_district_vitality",
        name_ko="상권 활성도",
        axis=FactorAxis.SENTIMENT,
    ),
    "rate_sensitivity": CommercialFactor(
        key="rate_sensitivity", name_ko="금리 민감도", axis=FactorAxis.SENTIMENT
    ),
    "rent_trend": CommercialFactor(
        key="rent_trend", name_ko="임대료 동향", axis=FactorAxis.SENTIMENT
    ),
    "tenant_stability": CommercialFactor(
        key="tenant_stability", name_ko="임차인 안정성", axis=FactorAxis.SENTIMENT
    ),
}
