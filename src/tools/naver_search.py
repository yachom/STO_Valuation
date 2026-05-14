"""네이버 통합검색 API. 블로그/뉴스/이미지/local 채널별 total을 수집."""

from __future__ import annotations

from typing import Any

import requests

from .. import config
from .http import ToolError

_BASE = "https://openapi.naver.com/v1/search"
_CHANNELS = ("blog", "news", "webkr", "image", "local", "cafearticle")


def _headers() -> dict[str, str]:
    if not (config.NAVER_CLIENT_ID and config.NAVER_CLIENT_SECRET):
        raise ToolError("NAVER_CLIENT_ID/SECRET missing")
    return {
        "X-Naver-Client-Id": config.NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET,
        "User-Agent": config.USER_AGENT,
    }


def _hit(channel: str, query: str, display: int = 5) -> dict[str, Any]:
    try:
        resp = requests.get(
            f"{_BASE}/{channel}.json",
            headers=_headers(),
            params={"query": query, "display": display},
            timeout=15,
        )
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "body": resp.text[:200]}
        return resp.json()
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def search_all_channels(query: str) -> dict[str, Any]:
    out: dict[str, Any] = {"query": query, "channels": {}}
    totals: dict[str, int] = {}
    for ch in _CHANNELS:
        data = _hit(ch, query)
        items = data.get("items", []) if isinstance(data, dict) else []
        total = data.get("total") if isinstance(data, dict) else None
        totals[ch] = total or 0
        out["channels"][ch] = {
            "total": total,
            "preview": [
                {
                    "title": (i.get("title") or "").replace("<b>", "").replace("</b>", ""),
                    "link": i.get("link"),
                    "description": (i.get("description") or "")[:200],
                }
                for i in items[:3]
            ],
            "error": data.get("error") if isinstance(data, dict) else None,
        }
    out["totals"] = totals
    out["aggregate_total"] = sum(totals.values())
    return out
