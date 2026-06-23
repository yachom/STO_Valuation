"""ddgs(DuckDuckGo 메타서치) — 키 불필요 범용 웹/뉴스 채널.

Tavily와 평행하나 무키. text=스니펫, news=뉴스, extract=본문 전문.
로거 컨벤션: external.ddgs.
"""

from infra.external.ddgs.client import DdgsClient, DdgsResult
from infra.external.ddgs.exceptions import DdgsError, DdgsRateLimited, DdgsTimeout

__all__ = ["DdgsClient", "DdgsResult", "DdgsError", "DdgsRateLimited", "DdgsTimeout"]
