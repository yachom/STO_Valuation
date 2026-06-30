"""DuckDuckGo(ddgs) 메타서치 — 키 불필요 범용 웹/뉴스 채널.

ddgs(구 duckduckgo_search)는 bing·brave·google·startpage·wikipedia 등 여러
검색엔진을 묶는 메타서치 라이브러리다. Tavily(키 필요·content 인라인)와 평행한
'범용 웹' 채널이되 **키가 필요 없고**, 라이브러리가 HTTP를 자체 처리한다 — 그래서
infra/external/_http(우리가 요청을 만드는 키 기반 API 패턴)를 쓰지 않고 DDGS 객체를
얇게 감싸되, 게이트웨이(레이트리밋)·로거(external.ddgs)·예외 래핑 컨벤션은 동일하게
따른다.

text()는 스니펫(body)만 주므로 전문이 필요하면 extract(url)로 본문을 가져온다.
라이브 엔진을 긁어 RatelimitException이 잦다 — backend='auto'(기본) + 게이트웨이
간격으로 완충한다. 무키. 로거 컨벤션: external.ddgs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from infra.external.base_client import BaseExternalClient
from infra.external.ddgs.exceptions import DdgsError, DdgsRateLimited, DdgsTimeout
from infra.gateway import APIGateway

logger = logging.getLogger("external.ddgs")


@dataclass
class DdgsResult:
    """ddgs 검색 결과 1건 — text/news 공통 정규화 형태."""
    title: str
    url: str
    body: str                 # text=스니펫 / news=요약
    published_at: str | None  # 항상 None — ddgs date는 불안정해 미신뢰(search 주석 참조)
    source: str | None        # news의 source (text는 None → host로 대체)

    @property
    def host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.url).netloc


class DdgsClient(BaseExternalClient):
    """ddgs DDGS 얇은 래퍼 (게이트웨이 + 예외 래핑 + audit). 무키."""

    API_NAME = "ddgs"
    api_name = "ddgs"

    def __init__(self, *, timeout: float = 10.0, region: str = "kr-ko",
                 safesearch: str = "moderate", backend: str = "auto",
                 proxy: str | None = None, gateway: APIGateway | None = None):
        super().__init__(gateway=gateway, timeout=timeout)
        self.timeout = timeout
        self.region = region
        self.safesearch = safesearch
        self.backend = backend
        self.proxy = proxy

    def _ddgs(self) -> DDGS:
        # 라이브러리 권장 사용은 호출당 인스턴스 — 내부 HTTP 클라이언트를 매번 새로
        # 잡는다(스레드 안전). 게이트웨이가 간격을 강제하므로 오버헤드는 무시 가능.
        kwargs: dict = {"timeout": self.timeout}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return DDGS(**kwargs)

    def search(self, query: str, *, topic: str = "text", max_results: int = 6,
               region: str | None = None, timelimit: str | None = None,
               backend: str | None = None) -> list[DdgsResult]:
        """범용 웹(topic='text') 또는 뉴스(topic='news') 검색.

        timelimit: d/w/m/y (최근 N일·주·월·년). region 미지정 시 기본(kr-ko).
        Returns: DdgsResult 리스트. 실패 시 DdgsError(레이트리밋/타임아웃 하위클래스).
        """
        kwargs: dict = {
            "region": region or self.region,
            "safesearch": self.safesearch,
            "max_results": max_results,
            "backend": backend or self.backend,
        }
        if timelimit:
            kwargs["timelimit"] = timelimit

        self._gateway.wait(self.API_NAME)
        method = "news" if topic == "news" else "text"
        with self._audited(method, request_summary=query):
            try:
                rows = getattr(self._ddgs(), method)(query, **kwargs)
            except RatelimitException as exc:
                raise DdgsRateLimited(f"ddgs 레이트리밋: {exc}", cause=exc) from exc
            except TimeoutException as exc:
                raise DdgsTimeout(f"ddgs 타임아웃: {exc}", cause=exc) from exc
            except DDGSException as exc:
                # ddgs는 결과 0건을 예외로 던진다 — 에러가 아니라 빈 결과이므로 []로 정상 처리
                # (안 그러면 harvester가 ddgs_search_error로 스킵, news 패스가 매번 헛돔).
                if "no results" in str(exc).lower():
                    logger.info("ddgs.%s '%s' → 0 results", method, query[:40])
                    return []
                raise DdgsError(f"ddgs {method} 실패: {exc}", cause=exc) from exc
            except Exception as exc:              # 라이브러리 내부 비표준 예외 방어
                raise DdgsError(f"ddgs {method} 오류: {exc}", cause=exc) from exc

            out: list[DdgsResult] = []
            for r in rows or []:
                url = r.get("url") or r.get("href") or ""
                if not url:
                    continue
                out.append(DdgsResult(
                    title=r.get("title") or "",
                    url=url,
                    body=r.get("body") or "",
                    # ddgs date는 백엔드마다 불안정(실측): ISO placeholder(2020-..+현재시각)나
                    # '6일 전' 류 상대문자열이 섞여 좋은 날짜를 가려낼 수 없다. 구조화 날짜로는
                    # 약속하지 않고 버린다 — 시점은 소비자가 본문에서 LLM으로 뽑는다.
                    published_at=None,
                    source=r.get("source"),
                ))
            logger.info("ddgs.%s '%s' → %d results", method, query[:40], len(out))
            return out

    def extract(self, url: str, *, fmt: str = "text_markdown") -> str:
        """URL 본문 추출 (HTML→마크다운/텍스트). 검색 스니펫의 전문 보강.

        fmt: text_markdown(기본)/text_plain/text_rich/text/content.
        Returns: 본문 문자열(없으면 ''). 실패 시 DdgsError.
        """
        self._gateway.wait(self.API_NAME)
        with self._audited("extract", endpoint=url):
            try:
                data = self._ddgs().extract(url, fmt)
            except RatelimitException as exc:
                raise DdgsRateLimited(f"ddgs 레이트리밋: {exc}", cause=exc) from exc
            except TimeoutException as exc:
                raise DdgsTimeout(f"ddgs 타임아웃: {exc}", cause=exc) from exc
            except DDGSException as exc:
                raise DdgsError(f"ddgs extract 실패: {exc}", cause=exc) from exc
            except Exception as exc:
                raise DdgsError(f"ddgs extract 오류: {exc}", cause=exc) from exc

            content = (data or {}).get("content")
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            logger.info("ddgs.extract '%s' → %d chars", url[:60], len(content or ""))
            return content or ""
