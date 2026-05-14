"""네이버 SearchAd - 키워드툴(월간 검색량)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

import requests

from .. import config

_BASE = "https://api.searchad.naver.com"


def _signature(timestamp: str, method: str, uri: str) -> str:
    msg = f"{timestamp}.{method}.{uri}"
    sig = hmac.new(
        config.NAVER_SEARCHAD_SECRET_KEY.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(sig).decode("utf-8")


def _headers(method: str, uri: str) -> dict[str, str]:
    ts = str(int(time.time() * 1000))
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Timestamp": ts,
        "X-API-KEY": config.NAVER_SEARCHAD_ACCESS_LICENSE,
        "X-Customer": config.NAVER_SEARCHAD_CUSTOMER_ID,
        "X-Signature": _signature(ts, method, uri),
        "User-Agent": config.USER_AGENT,
    }


def keyword_tool(hint_keyword: str) -> dict[str, Any]:
    if not (
        config.NAVER_SEARCHAD_CUSTOMER_ID
        and config.NAVER_SEARCHAD_ACCESS_LICENSE
        and config.NAVER_SEARCHAD_SECRET_KEY
    ):
        return {"skipped": "SearchAd credentials missing"}

    uri = "/keywordstool"
    try:
        resp = requests.get(
            _BASE + uri,
            headers=_headers("GET", uri),
            params={"hintKeywords": hint_keyword, "showDetail": "1"},
            timeout=15,
        )
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "body": resp.text[:300]}
        data = resp.json() or {}
        items = data.get("keywordList", [])[:30]
        summary = {
            "hint_keyword": hint_keyword,
            "total_returned": len(items),
            "items": [
                {
                    "keyword": i.get("relKeyword"),
                    "pc_search": i.get("monthlyPcQcCnt"),
                    "mobile_search": i.get("monthlyMobileQcCnt"),
                    "ad_competition": i.get("compIdx"),
                }
                for i in items
            ],
        }
        return summary
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}
