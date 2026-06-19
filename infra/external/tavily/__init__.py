"""Tavily 웹 검색 — 네이버 외 멀티소스 웹/뉴스 채널.

content를 인라인 반환(스크랩 불필요)하는 LLM 친화 검색 API. 무료 1,000요청/월.
키(TAVILY_API_KEY) 없으면 상위(harvester)에서 graceful skip.
"""

from infra.external.tavily.client import TavilyClient, TavilyError, WebResult

__all__ = ["TavilyClient", "TavilyError", "WebResult"]
