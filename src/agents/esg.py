"""ESG Agent.

데이터:
- G-SEED 인증 현황
- 건축물대장(사용승인일=노후도, 주용도, 위반건축물 여부)
- 토지이용계획(녹지/보존)
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..tools import gseed
from . import _llm

SYSTEM = """당신은 부동산 STO(증권형 토큰)의 사회적 가치 평가 중 'ESG' 축을 담당하는 평가 에이전트다.
- E(Environment): 친환경 인증(G-SEED 등급/구분), 건물 노후도, 위치한 용도지역의 환경성
- S(Social): 공공기여(주용도, 위반건축물 여부, 공익적 입지)
- G(Governance): 데이터 자체로 평가 불가시 'N/A 영역'으로 명시
한국 부동산 시장의 관점에서 보수적이고 근거 기반으로 평가하라.
점수는 모든 데이터가 결측이면 50을 기본으로 두고 강한 근거가 있을 때만 가감하라.
"""


def _extract_age_years(building_pack: dict[str, Any]) -> int | None:
    title = (building_pack or {}).get("title") or {}
    items = _items_of(title)
    for it in items:
        use_apr = it.get("useAprDay") or it.get("useAprYmd")
        if use_apr:
            try:
                y = int(str(use_apr)[:4])
                return date.today().year - y
            except Exception:
                return None
    return None


def _items_of(api_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """공공API response.body.items.item 추출 best-effort."""
    if not isinstance(api_payload, dict):
        return []
    resp = api_payload.get("response") or api_payload
    body = (resp or {}).get("body") if isinstance(resp, dict) else None
    items = (body or {}).get("items") if isinstance(body, dict) else None
    if isinstance(items, dict):
        items = items.get("item")
    if items is None:
        return []
    if isinstance(items, list):
        return items
    return [items]


def _summarize_building(building_pack: dict[str, Any]) -> dict[str, Any]:
    title_items = _items_of((building_pack or {}).get("title") or {})
    if not title_items:
        return {"available": False}
    first = title_items[0]
    return {
        "available": True,
        "main_purpose": first.get("mainPurpsCdNm"),
        "structure": first.get("strctCdNm"),
        "floors_above": first.get("grndFlrCnt"),
        "floors_below": first.get("ugrndFlrCnt"),
        "total_floor_area_m2": first.get("totArea"),
        "use_approval_day": first.get("useAprDay"),
        "violation_yn": first.get("vltnBldYn"),
        "elevator_passenger": first.get("rideUseElvtCnt"),
        "elevator_emergency": first.get("emgenUseElvtCnt"),
    }


def _summarize_land(land_pack: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    use_items = _items_of((land_pack or {}).get("land_use") or {})
    if use_items:
        out["land_use"] = [
            {"zone": i.get("prposAreaDstrcCodeNm"), "category": i.get("prposAreaDstrcCode2Nm")}
            for i in use_items[:5]
        ]
    char_items = _items_of((land_pack or {}).get("land_characteristics") or {})
    if char_items:
        c = char_items[0]
        out["land_characteristics"] = {
            "지목": c.get("lndcgrCodeNm"),
            "면적": c.get("lndpclAr"),
            "용도지역": c.get("prposArea1Nm"),
        }
    price_items = _items_of((land_pack or {}).get("official_land_price") or {})
    if price_items:
        out["official_land_price_recent"] = price_items[0]
    return out


def run(metadata: dict[str, Any], callbacks: list[Any] | None = None) -> dict[str, Any]:
    address = metadata.get("address", {})
    keyword = address.get("road") or address.get("jibun") or metadata.get("input_address") or ""

    gseed_result = gseed.search_by_address(keyword)
    age_years = _extract_age_years(metadata.get("building", {}))
    building_summary = _summarize_building(metadata.get("building", {}))
    land_summary = _summarize_land(metadata.get("land", {}))

    evidence = {
        "gseed": gseed_result,
        "building_age_years": age_years,
        "building_summary": building_summary,
        "land_summary": land_summary,
    }

    scored = _llm.score_with_llm(
        SYSTEM, evidence, axis_label="ESG (환경·사회·거버넌스)", callbacks=callbacks
    )
    return {"axis": "ESG", "evidence": evidence, **scored}
