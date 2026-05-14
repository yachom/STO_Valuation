"""소상공인진흥공단 상권정보 API.

활용신청 승인 후 사용 가능. 미승인 시 graceful skip.
주변 반경 내 업종/점포 분포를 조회한다.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from .. import config
from .http import request_json


_BASE = "https://apis.data.go.kr/B553077/api/open/sdsc2"


def _service_key() -> str | None:
    if not config.SBIZ_SERVICE_KEY:
        return None
    return urllib.parse.unquote(config.SBIZ_SERVICE_KEY)


def stores_in_radius(cx: float | str, cy: float | str, radius_m: int = 300) -> dict[str, Any]:
    """좌표 반경 내 상가 업소 조회 (storeListInRadius).

    cx=경도(WGS84), cy=위도(WGS84).
    """
    key = _service_key()
    if not key:
        return {"skipped": "SBIZ_SERVICE_KEY missing"}

    try:
        data = request_json(
            f"{_BASE}/storeListInRadius",
            {
                "ServiceKey": key,
                "radius": radius_m,
                "cx": cx,
                "cy": cy,
                "type": "json",
                "numOfRows": 100,
                "pageNo": 1,
            },
        )
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}

    body = (data.get("body") if isinstance(data, dict) else None) or data
    items = (body or {}).get("items") or []
    if isinstance(items, dict):
        items = items.get("item", []) or []

    by_industry: dict[str, int] = {}
    by_large: dict[str, int] = {}
    for it in items:
        ind = it.get("indsLclsNm") or it.get("indsMclsNm") or "기타"
        by_large[ind] = by_large.get(ind, 0) + 1
        small = it.get("indsSclsNm") or it.get("indsMclsNm") or "기타"
        by_industry[small] = by_industry.get(small, 0) + 1

    return {
        "radius_m": radius_m,
        "total_count": len(items),
        "by_large_industry": dict(sorted(by_large.items(), key=lambda x: -x[1])),
        "by_small_industry": dict(sorted(by_industry.items(), key=lambda x: -x[1])[:30]),
        "samples": items[:10],
    }
