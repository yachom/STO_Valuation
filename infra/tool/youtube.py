"""YouTube 검색 도구 — yt-dlp public search channel.

출력 items는 Tavily/DDGS와 같은 title/link/content/pub_date/source shape를 따른다.
yt-dlp는 optional dependency이므로 사용처에서 lazy import하고 실패 시 skip한다.
"""

from __future__ import annotations

import logging

from infra.external.youtube import YouTubeClient
from infra.tool.base import BaseTool

logger = logging.getLogger("graphflow.tool.youtube")


class YouTubeSearchTool(BaseTool):
    """검색어 → YouTube 영상 검색 결과. content는 설명란 + 공개 메타데이터."""

    name = "youtube_search"

    def __init__(self):
        super().__init__()
        self._client = YouTubeClient()

    def _run(self, *, query: str, max_results: int = 5):
        results = self._client.search(query, max_results=max_results)
        return {
            "query": query,
            "n_results": len(results),
            "items": [
                {
                    "title": r.title,
                    "link": r.url,
                    "content": r.content,
                    "pub_date": r.published_at,
                    "source": r.source,
                    "channel": r.channel,
                    "video_id": r.video_id,
                    "view_count": r.view_count,
                    "like_count": r.like_count,
                }
                for r in results
                if r.url
            ],
        }
