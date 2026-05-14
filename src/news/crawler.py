"""뉴스 자동 수집.

네이버 뉴스 검색 API로 다중 쿼리를 돌려 중복 제거 후 반환.
- 쿼리는 자산명/도로명/지번/시군구+자산명 등 후보 조합
- 최근 N개월 이내만 통과 (헤더 pubDate 기반)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from .. import config

_URL = "https://openapi.naver.com/v1/search/news.json"


def _headers() -> dict[str, str]:
    return {
        "X-Naver-Client-Id": config.NAVER_CLIENT_ID or "",
        "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET or "",
        "User-Agent": config.USER_AGENT,
    }


def _strip_tags(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).replace("&quot;", '"').replace("&amp;", "&")


def _parse_pubdate(pub: str | None) -> datetime | None:
    if not pub:
        return None
    try:
        return parsedate_to_datetime(pub)
    except Exception:
        return None


def _candidate_queries(metadata: dict[str, Any]) -> list[str]:
    addr = metadata.get("address", {}) or {}
    name = addr.get("building_name")
    sigungu = addr.get("sigungu")
    bjdong = addr.get("bjdong_name")
    jibun = addr.get("jibun")
    road = addr.get("road")

    queries: list[str] = []
    if name:
        queries.append(name)
        if sigungu:
            queries.append(f"{sigungu} {name}")
        queries.extend([f"{name} 매각", f"{name} 임대", f"{name} 리모델링"])
    if road:
        queries.append(road)
    if jibun:
        queries.append(jibun)
    if sigungu and bjdong:
        queries.append(f"{sigungu} {bjdong} 부동산")
    return list(dict.fromkeys(q for q in queries if q))[:8]


def fetch_news(metadata: dict[str, Any], months: int = 12, per_query: int = 30) -> dict[str, Any]:
    if not (config.NAVER_CLIENT_ID and config.NAVER_CLIENT_SECRET):
        return {"skipped": "NAVER credentials missing", "items": [], "queries": []}

    queries = _candidate_queries(metadata)
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 31)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    errors: list[str] = []

    for q in queries:
        try:
            resp = requests.get(
                _URL,
                headers=_headers(),
                params={"query": q, "display": min(per_query, 100), "sort": "date"},
                timeout=15,
            )
            if resp.status_code >= 400:
                errors.append(f"{q}: HTTP {resp.status_code}")
                continue
            for raw in resp.json().get("items", []):
                link = raw.get("link") or raw.get("originallink")
                if not link or link in seen:
                    continue
                pub_dt = _parse_pubdate(raw.get("pubDate"))
                if pub_dt and pub_dt < cutoff:
                    continue
                seen.add(link)
                items.append(
                    {
                        "title": _strip_tags(raw.get("title")),
                        "description": _strip_tags(raw.get("description")),
                        "link": link,
                        "originallink": raw.get("originallink"),
                        "pubDate": raw.get("pubDate"),
                        "query": q,
                    }
                )
        except Exception as exc:
            errors.append(f"{q}: {type(exc).__name__}: {exc}")

    items.sort(key=lambda x: x.get("pubDate") or "", reverse=True)
    return {
        "queries": queries,
        "total_unique": len(items),
        "items": items[:80],
        "errors": errors,
    }
