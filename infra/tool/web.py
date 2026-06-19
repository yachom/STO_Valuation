"""웹 검색 도구 — Tavily(멀티소스 웹/뉴스). infra/tool/naver와 평행.

infra/external/tavily 클라이언트를 BaseTool로 감싸 트레이싱을 자동으로 얻는다.
네이버(한국 뉴스 특화) 외 일반 웹/해외 소스를 넓혀 '볼 수 있는 소스 수'를 늘린다.
content를 인라인 반환하므로 별도 스크랩 도구가 불필요하다.
"""

from __future__ import annotations

import logging

from infra.external.tavily import TavilyClient
from infra.tool.base import BaseTool

logger = logging.getLogger("graphflow.tool.web")


class TavilyWebSearchTool(BaseTool):
    """검색어 → 웹/뉴스 결과(title/link/content/pub_date/source). content 인라인."""

    name = "tavily_web_search"

    def __init__(self, api_key: str):
        super().__init__()
        self._client = TavilyClient(api_key)

    def _run(self, *, query: str, max_results: int = 5,
             topic: str = "news", days: int | None = None):
        results = self._client.search(query, max_results=max_results,
                                      topic=topic, days=days)
        return {
            "query": query, "n_results": len(results),
            "items": [{"title": r.title, "link": r.url, "content": r.content,
                       "pub_date": r.published_at, "source": r.host, "score": r.score}
                      for r in results],
        }
