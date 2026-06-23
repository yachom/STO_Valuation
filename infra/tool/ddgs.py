"""ddgs 웹 검색/본문추출 도구 — infra/tool/web(Tavily)과 평행, 무키 채널.

infra/external/ddgs 클라이언트를 BaseTool로 감싸 트레이싱(Langfuse tool span +
로컬 사이드카)을 자동으로 얻는다. Tavily(키 필요)와 독립으로 공존하는 범용 웹
채널 — 키 없이도 동작하며 해외/일반 웹 소스를 넓힌다. text는 스니펫만 주므로,
전문이 필요하면 DdgsExtractTool로 본문을 가져온다.

출력 items 형태는 TavilyWebSearchTool과 동일(title/link/content/pub_date/source)
— harvester가 두 채널을 같은 코드로 처리한다.
"""

from __future__ import annotations

import logging

from infra.external.ddgs import DdgsClient, DdgsError
from infra.tool.base import BaseTool

logger = logging.getLogger("graphflow.tool.ddgs")


class DdgsSearchTool(BaseTool):
    """검색어 → 범용 웹(topic='text')/뉴스(topic='news') 결과. content는 스니펫. 무키."""

    name = "ddgs_search"

    def __init__(self):
        super().__init__()
        self._client = DdgsClient()

    def _run(self, *, query: str, max_results: int = 6, topic: str = "text",
             region: str | None = None, timelimit: str | None = None):
        results = self._client.search(query, topic=topic, max_results=max_results,
                                      region=region, timelimit=timelimit)
        return {
            "query": query, "n_results": len(results),
            "items": [{"title": r.title, "link": r.url, "content": r.body,
                       "pub_date": r.published_at, "source": r.source or r.host}
                      for r in results],
        }


class DdgsExtractTool(BaseTool):
    """URL → 본문(마크다운/텍스트). 검색 스니펫의 전문 보강."""

    name = "ddgs_extract"

    def __init__(self):
        super().__init__()
        self._client = DdgsClient()

    def _run(self, *, url: str, fmt: str = "text_markdown"):
        try:
            content = self._client.extract(url, fmt=fmt)
        except DdgsError as exc:
            # 차단/레이트리밋 등은 정상 흐름의 일부 — 호출자가 skip 판단
            return {"url": url, "ok": False, "error": str(exc)[:200]}
        return {"url": url, "ok": True, "content": content}
