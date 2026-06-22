"""Tavily Search API HTTP 클라이언트.

네이버(한국 뉴스 특화) 외에 일반 웹/뉴스를 멀티소스로 넓히는 검색 채널.
Tavily는 결과에 content(정제 본문 일부)를 인라인으로 주므로 별도 스크랩이
불필요하다 — harvester가 content를 바로 Fact 추출에 쓴다.

엔드포인트: POST https://api.tavily.com/search
인증: Authorization: Bearer {TAVILY_API_KEY}
무료 티어 1,000 크레딧/월. 로거 컨벤션: external.tavily.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

from infra.external import _http
from infra.gateway import APIGateway, get_default

logger = logging.getLogger("external.tavily")


class TavilyError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


@dataclass
class WebResult:
    """웹 검색 결과 1건 — content가 인라인이라 스크랩 불필요."""
    title: str
    url: str
    content: str
    published_at: str | None
    score: float | None

    @property
    def host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.url).netloc


class TavilyClient:
    """Tavily Search API thin wrapper (urllib + 게이트웨이)."""

    API_NAME = "tavily"
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, *, timeout: float = 25.0,
                 gateway: APIGateway | None = None):
        self.api_key = api_key
        self.timeout = timeout
        self._gateway = gateway if gateway is not None else get_default()

    def search(self, query: str, *, max_results: int = 5,
               topic: str = "news", days: int | None = None,
               search_depth: str = "basic") -> list[WebResult]:
        """웹 검색. topic='news'면 뉴스 우선, days로 최근 N일 한정.

        Returns: WebResult 리스트 (content 인라인). 실패 시 TavilyError.
        """
        body: dict = {
            "query": query, "max_results": max_results,
            "search_depth": search_depth, "topic": topic,
            "include_answer": False, "include_raw_content": False,
        }
        if days is not None:
            body["days"] = days
        req = urllib.request.Request(
            self.URL,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        _, raw, _ = _http.timed_urlopen(
            req,
            api_name=self.API_NAME,
            gateway=self._gateway,
            timeout=self.timeout,
            on_http_error=lambda status, b: TavilyError(
                f"Tavily HTTP {status}: {b[:160]}", status=status),
            on_url_error=lambda reason: TavilyError(f"네트워크 오류: {reason}"),
            data=json.dumps(body).encode("utf-8"),
        )
        data = _http.parse_json(
            raw, on_error=lambda exc: TavilyError(f"JSON 파싱 실패: {raw[:160]}"))

        out: list[WebResult] = []
        for r in data.get("results", []):
            url = r.get("url")
            if not url:
                continue
            out.append(WebResult(
                title=r.get("title") or "",
                url=url,
                content=r.get("content") or "",
                published_at=r.get("published_date"),
                score=r.get("score"),
            ))
        logger.info("tavily '%s' → %d results", query[:40], len(out))
        return out
