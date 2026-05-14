"""건축물대장."""

from __future__ import annotations

import urllib.parse
from typing import Any

from .. import config
from .http import request_json


def _plat_gb_cd(land_type_code: str) -> str:
    return "0" if land_type_code == "1" else "1"


def _service_key() -> str | None:
    if not config.MOLIT_SERVICE_KEY:
        return None
    return urllib.parse.unquote(config.MOLIT_SERVICE_KEY)


def _call(endpoint: str, ids: dict[str, str]) -> Any:
    key = _service_key()
    if not key:
        return {"skipped": "MOLIT_SERVICE_KEY missing"}
    return request_json(
        f"{config.BUILDING_REGISTER_BASE_URL}/{endpoint}",
        {
            "serviceKey": key,
            "sigunguCd": ids["sigungu_cd"],
            "bjdongCd": ids["bjdong_cd"],
            "platGbCd": _plat_gb_cd(ids["land_type_code"]),
            "bun": ids["bun"],
            "ji": ids["ji"],
            "_type": "json",
            "numOfRows": 20,
            "pageNo": 1,
        },
    )


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def building_pack(ids: dict[str, str]) -> dict[str, Any]:
    return {
        "basis": _safe(_call, "getBrBasisOulnInfo", ids),
        "title": _safe(_call, "getBrTitleInfo", ids),
        "recap_title": _safe(_call, "getBrRecapTitleInfo", ids),
        "floor": _safe(_call, "getBrFlrOulnInfo", ids),
        "zone": _safe(_call, "getBrJijiguInfo", ids),
        "exclusive_section": _safe(_call, "getBrExposInfo", ids),
    }
