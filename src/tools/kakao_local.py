"""카카오 로컬: 주소→좌표, 반경 카테고리 POI 카운트."""

from __future__ import annotations

from typing import Any

import requests

from .. import config

_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
_CATEGORY_URL = "https://dapi.kakao.com/v2/local/search/category.json"
_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"

CATEGORY_CODES = {
    "대형마트": "MT1",
    "편의점": "CS2",
    "어린이집_유치원": "PS3",
    "학교": "SC4",
    "학원": "AC5",
    "주차장": "PK6",
    "주유소_충전소": "OL7",
    "지하철역": "SW8",
    "은행": "BK9",
    "문화시설": "CT1",
    "중개업소": "AG2",
    "공공기관": "PO3",
    "관광명소": "AT4",
    "숙박": "AD5",
    "음식점": "FD6",
    "카페": "CE7",
    "병원": "HP8",
    "약국": "PM9",
}


def _headers() -> dict[str, str]:
    if not config.KAKAO_REST_API_KEY:
        return {}
    return {
        "Authorization": f"KakaoAK {config.KAKAO_REST_API_KEY}",
        "User-Agent": config.USER_AGENT,
    }


def address_to_coord(address: str) -> dict[str, Any] | None:
    if not config.KAKAO_REST_API_KEY:
        return None
    try:
        resp = requests.get(
            _ADDRESS_URL, headers=_headers(), params={"query": address}, timeout=15
        )
        if resp.status_code >= 400:
            return None
        docs = resp.json().get("documents", [])
        if not docs:
            return None
        d = docs[0]
        return {"x": d.get("x"), "y": d.get("y"), "raw": d}
    except Exception:
        return None


def _category_count(code: str, x: str, y: str, radius: int) -> dict[str, Any]:
    """Return total count + a few examples for a category at coord."""
    try:
        resp = requests.get(
            _CATEGORY_URL,
            headers=_headers(),
            params={
                "category_group_code": code,
                "x": x,
                "y": y,
                "radius": radius,
                "sort": "distance",
                "page": 1,
                "size": 15,
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "total_count": 0, "samples": []}
        data = resp.json()
        meta = data.get("meta", {})
        docs = data.get("documents", [])
        return {
            "total_count": meta.get("total_count", len(docs)),
            "samples": [
                {
                    "name": d.get("place_name"),
                    "category": d.get("category_name"),
                    "distance_m": d.get("distance"),
                }
                for d in docs[:5]
            ],
        }
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc), "total_count": 0, "samples": []}


def poi_radius_summary(x: str, y: str, radius: int = 300) -> dict[str, Any]:
    if not config.KAKAO_REST_API_KEY or not (x and y):
        return {"skipped": "KAKAO key or coord missing"}
    summary: dict[str, Any] = {"radius_m": radius, "x": x, "y": y, "by_category": {}}
    for name, code in CATEGORY_CODES.items():
        summary["by_category"][name] = _category_count(code, x, y, radius)
    summary["category_totals"] = {
        name: payload.get("total_count", 0)
        for name, payload in summary["by_category"].items()
    }
    summary["overall_total"] = sum(summary["category_totals"].values())
    return summary


def keyword_search(query: str, x: str | None = None, y: str | None = None, radius: int = 1000) -> dict[str, Any]:
    if not config.KAKAO_REST_API_KEY:
        return {"skipped": "KAKAO key missing"}
    params: dict[str, Any] = {"query": query, "size": 15}
    if x and y:
        params.update({"x": x, "y": y, "radius": radius, "sort": "distance"})
    try:
        resp = requests.get(_KEYWORD_URL, headers=_headers(), params=params, timeout=15)
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "body": resp.text[:200]}
        data = resp.json()
        return {
            "total_count": data.get("meta", {}).get("total_count", 0),
            "documents": [
                {
                    "place_name": d.get("place_name"),
                    "category": d.get("category_name"),
                    "address": d.get("road_address_name") or d.get("address_name"),
                    "phone": d.get("phone"),
                    "distance_m": d.get("distance"),
                }
                for d in data.get("documents", [])[:10]
            ],
        }
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
