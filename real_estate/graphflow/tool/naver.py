"""네이버 뉴스 검색/스크랩 — graphflow 툴 래퍼.

infra/external의 검증된 클라이언트(naver_search, naver_news_scraper)를 BaseTool로
감싸 트레이싱(Langfuse tool span + 로컬 사이드카)을 자동으로 얻는다. 게이트웨이
(infra/gateway)는 클라이언트 내부에서 통과한다.

PR-C 범위: 뉴스만 (D8 토의 확정). blog/webkr 채널은 후속.
"""

from __future__ import annotations

import logging

from infra.external.naver_news_scraper import NewsScrapeError, fetch_news_article
from infra.external.naver_search import (
    NaverSearchClient,
    fetch_news_metadata,
    load_credentials,
)
from real_estate.graphflow.tool.base import BaseTool

logger = logging.getLogger("graphflow.tool.naver")

_client: NaverSearchClient | None = None


def _get_client() -> NaverSearchClient:
    global _client
    if _client is None:
        _client = NaverSearchClient(load_credentials())
    return _client


class NaverNewsSearchTool(BaseTool):
    """검색어 → 네이버 뉴스 메타데이터 리스트 (title/description/link/pub_date)."""

    name = "naver_news_search"

    def _run(self, *, query: str, display: int = 10, sort: str = "sim"):
        items = fetch_news_metadata(_get_client(), query, display=display, sort=sort)
        return {
            "query": query,
            "n_results": len(items),
            "items": [it.to_dict() for it in items],
        }


class NaverNewsScrapeTool(BaseTool):
    """네이버 뉴스 URL → 본문/제목/언론사/발행일. 네이버 뉴스 호스트만 지원."""

    name = "naver_news_scrape"

    def _run(self, *, url: str):
        try:
            article = fetch_news_article(url)
        except NewsScrapeError as exc:
            # 미지원 호스트/차단 등은 정상 흐름의 일부 — 호출자가 skip 판단
            return {"url": url, "ok": False, "error": str(exc)[:200]}
        return {"url": url, "ok": True, **article.to_dict()}
