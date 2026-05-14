"""행안부 도로명주소 API."""

from __future__ import annotations

from typing import Any

from .. import config
from .http import ToolError, request_json


def search_address(keyword: str, limit: int = 5) -> dict[str, Any]:
    if not config.JUSO_CONFIRM_KEY:
        raise ToolError("JUSO_CONFIRM_KEY is missing.")
    data = request_json(
        "https://business.juso.go.kr/addrlink/addrLinkApi.do",
        {
            "confmKey": config.JUSO_CONFIRM_KEY,
            "currentPage": 1,
            "countPerPage": limit,
            "keyword": keyword,
            "resultType": "json",
        },
    )
    rows = data.get("results", {}).get("juso", []) or []
    if not rows:
        raise ToolError(f"No JUSO result for: {keyword}")
    return {"top": rows[0], "candidates": rows}


def normalize_lot(main: Any, sub: Any = 0) -> tuple[str, str]:
    main_text = str(main or "0").strip()
    sub_text = str(sub or "0").strip()
    return main_text.zfill(4), sub_text.zfill(4)


def pnu_from_juso_row(row: dict[str, Any]) -> str:
    adm_cd = str(row.get("admCd") or "")[:10]
    mt_yn = str(row.get("mtYn") or "0")
    land_type = "2" if mt_yn == "1" else "1"
    main, sub = normalize_lot(row.get("lnbrMnnm", "0"), row.get("lnbrSlno", "0"))
    return f"{adm_cd}{land_type}{main}{sub}"


def split_pnu(pnu: str) -> dict[str, str]:
    if len(pnu) != 19 or not pnu.isdigit():
        raise ToolError(f"PNU must be 19 digits: {pnu}")
    return {
        "pnu": pnu,
        "bjdong_code": pnu[:10],
        "sigungu_cd": pnu[:5],
        "bjdong_cd": pnu[5:10],
        "land_type_code": pnu[10],
        "bun": pnu[11:15],
        "ji": pnu[15:19],
    }
