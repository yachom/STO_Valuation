"""상권·입지 활성도 Agent.

데이터:
- 카카오 로컬 카테고리 반경 POI 카운트 (300m/500m)
- 소상공인진흥공단 상권정보 (반경 내 업소)
- 국토부 실거래가 (시군구 단위)
- VWorld 개별공시지가
"""

from __future__ import annotations

from typing import Any

from ..tools import kakao_local, molit_trade, sbiz
from . import _llm

SYSTEM = """당신은 부동산 STO 사회적 가치 평가 중 '상권·입지 활성도(Commercial Vitality)' 축을 담당한다.
다음 신호로 평가하라:
- 반경 내 다양한 업종 POI 밀도(카카오 로컬 카테고리, 소상공인 상권정보)
- 지하철역/대중교통 접근성(POI 카운트의 SW8/주차장 등)
- 실거래가 활성도(거래 건수, 가격 추세)
- 공시지가 수준
강한 신호:
- 음식점/카페/은행/지하철역/병원이 반경 300m에 다수 존재
- 최근 12개월 실거래가 다수 발생
- 공시지가 상위권
점수는 0~100으로 보수적으로 매겨라.
"""


def run(metadata: dict[str, Any], callbacks: list[Any] | None = None) -> dict[str, Any]:
    coord = metadata.get("coord") or {}
    x, y = coord.get("x"), coord.get("y")

    poi_300 = kakao_local.poi_radius_summary(x, y, radius=300) if x and y else {"skipped": "no coord"}
    poi_500 = kakao_local.poi_radius_summary(x, y, radius=500) if x and y else {"skipped": "no coord"}
    sbiz_300 = sbiz.stores_in_radius(x, y, radius_m=300) if x and y else {"skipped": "no coord"}

    sigungu_cd = (metadata.get("identifiers") or {}).get("sigungu_cd")
    trade = (
        molit_trade.fetch_transactions(sigungu_cd, property_type="상업업무용", trade_type="매매", months=12)
        if sigungu_cd
        else {"skipped": "no sigungu_cd"}
    )

    evidence = {
        "kakao_poi_radius_300m": poi_300,
        "kakao_poi_radius_500m_totals": (poi_500 or {}).get("category_totals"),
        "sbiz_radius_300m": sbiz_300,
        "molit_transactions_12mo": trade,
        "land_price": (metadata.get("land") or {}).get("official_land_price"),
    }

    scored = _llm.score_with_llm(
        SYSTEM, evidence, axis_label="상권·입지 활성도", callbacks=callbacks
    )
    return {"axis": "VITALITY", "evidence": evidence, **scored}
