"""G-SEED 녹색건축 인증현황 (odcloud)."""

from __future__ import annotations

import re
from typing import Any

from .. import config
from .http import request_json

_BASE = "https://api.odcloud.kr/api/15003214/v1/uddi:590f870b-8aef-4ac0-a08e-a36bbbdb01ec"
_CACHE: list[dict[str, Any]] | None = None


def _normalize(value: Any) -> str:
    text = str(value or "").lower().replace(" ", " ")
    return re.sub(r"\s+", "", text)


def _fetch_all(per_page: int = 1000) -> list[dict[str, Any]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not config.GSEED_SERVICE_KEY:
        return []

    all_rows: list[dict[str, Any]] = []
    page = 1
    while True:
        data = request_json(
            _BASE,
            {
                "page": page,
                "perPage": per_page,
                "serviceKey": config.GSEED_SERVICE_KEY,
                "returnType": "JSON",
            },
        )
        rows = data.get("data", []) or []
        if not rows:
            break
        all_rows.extend(rows)
        if len(all_rows) >= data.get("totalCount", 0):
            break
        page += 1

    _CACHE = all_rows
    return all_rows


def search_by_address(address_keyword: str) -> dict[str, Any]:
    rows = _fetch_all()
    if not rows:
        return {"status": "api_unavailable", "matches": []}

    key = _normalize(address_keyword)
    matches: list[dict[str, Any]] = []
    for row in rows:
        addr = (
            row.get("건축물 주소")
            or row.get("건축물주소")
            or row.get("주소")
            or ""
        )
        if key and key in _normalize(addr):
            matches.append(
                {
                    "건축물명": row.get("건축물명") or row.get("건물명"),
                    "주소": addr,
                    "인증등급": row.get("인증등급"),
                    "인증구분": row.get("인증구분"),
                    "인증기준": row.get("인증기준"),
                    "인증일자": row.get("인증일자"),
                    "인증번호": row.get("인증번호"),
                }
            )

    status = "certified" if matches else "not_found"
    representative = None
    if matches:
        for prio in ("본인증", "예비인증"):
            for m in matches:
                if m.get("인증구분") == prio:
                    representative = m
                    break
            if representative:
                break
        representative = representative or matches[0]

    return {"status": status, "matches": matches, "representative": representative}
