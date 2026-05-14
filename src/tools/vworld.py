"""VWorld 토지·공시지가."""

from __future__ import annotations

from typing import Any

from .. import config
from .http import request_json


def _call(endpoint: str, pnu: str) -> Any:
    if not config.VWORLD_API_KEY:
        return {"skipped": "VWORLD_API_KEY missing"}
    return request_json(
        f"https://api.vworld.kr/ned/data/{endpoint}",
        {
            "key": config.VWORLD_API_KEY,
            "domain": config.VWORLD_DOMAIN,
            "pnu": pnu,
            "format": "json",
            "numOfRows": 10,
            "pageNo": 1,
        },
    )


def land_pack(pnu: str) -> dict[str, Any]:
    return {
        "land_forest_ledger": _safe(_call, "ladfrlList", pnu),
        "land_characteristics": _safe(_call, "getLandCharacteristics", pnu),
        "land_use": _safe(_call, "getLandUseAttr", pnu),
        "official_land_price": _safe(_call, "getIndvdLandPriceAttr", pnu),
    }


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
