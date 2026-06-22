"""네이버 검색/스크랩 — graphflow 툴 래퍼.

infra/external의 검증된 클라이언트(naver_search, naver_news_scraper)를 BaseTool로
감싸 트레이싱(Langfuse tool span + 로컬 사이드카)을 자동으로 얻는다. 게이트웨이
(infra/gateway)는 클라이언트 내부에서 통과한다.

vertical:
    - news: 검색 + 본문 스크랩(NaverNewsSearchTool/NaverNewsScrapeTool).
    - blog/cafearticle/webkr/kin: 스크랩 없이 description 스니펫을 본문으로 직접
      사용(NaverVerticalSearchTool). 출처 신뢰 prior는 harvester가 부여한다.
"""

from __future__ import annotations

import logging

from infra.external.naver_news_scraper import NewsScrapeError, fetch_news_article
from infra.external.naver_search import (
    NaverSearchClient,
    fetch_news_metadata,
    fetch_vertical_metadata,
    load_credentials,
)
from infra.tool.base import BaseTool

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


class NaverVerticalSearchTool(BaseTool):
    """검색어 + vertical → 비뉴스 스니펫 리스트(blog/cafearticle/webkr/kin).

    description(스니펫)이 곧 문서 본문이라 스크랩 도구가 불필요하다(Tavily content
    인라인과 동일). 반환 shape는 news/Tavily 툴과 평행하게 맞춰 harvester가 인라인
    경로로 균일 처리한다. n_results==0이어도 정상(스킵은 호출자 판단).
    """

    name = "naver_vertical_search"

    def _run(self, *, query: str, vertical: str, display: int = 10, sort: str = "sim"):
        items = fetch_vertical_metadata(
            _get_client(), query, vertical=vertical, display=display, sort=sort)
        return {
            "query": query,
            "vertical": vertical,
            "n_results": len(items),
            "items": [it.to_dict() for it in items],
        }
