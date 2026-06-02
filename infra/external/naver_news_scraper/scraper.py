"""네이버 뉴스 URL → 본문/제목/언론사/발행일 크롤링.

Naver Open API가 아니라 n.news.naver.com 등의 HTML 페이지를 직접 긁는다.
API 키를 쓰지 않으며, NaverSearchClient와 무관하게 단독으로 사용 가능.

로거 컨벤션: external.{provider} 패턴을 따라 external.naver_news_scraper.
"""

from __future__ import annotations

import html
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from infra.repository.external.naver_news_scraper.exceptions import NewsScrapeError

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

logger = logging.getLogger("external.naver_news_scraper")

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# 네이버 뉴스 호스트만 허용 (외부 언론사 원문 URL은 사이트별 구조가 달라 지원 X)
_ALLOWED_HOSTS = {"news.naver.com", "n.news.naver.com", "m.news.naver.com"}

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class NewsArticle:
    url: str
    title: str
    press: str
    published_at: str
    body: str

    def to_dict(self) -> dict:
        return asdict(self)


def _is_naver_news(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in _ALLOWED_HOSTS


def _fetch(url: str, timeout: float) -> str:
    req = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
    started_at = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.info("GET %s -> %s (%dms)", url, resp.getcode(), elapsed_ms)
            return text
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.warning("GET %s -> HTTP %s (%dms)", url, exc.code, elapsed_ms)
        raise NewsScrapeError(f"HTTP {exc.code}", url=url, status=exc.code) from exc
    except urllib.error.URLError as exc:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.warning("GET %s -> 네트워크 오류 (%dms): %s", url, elapsed_ms, exc.reason)
        raise NewsScrapeError(f"네트워크 오류: {exc.reason}", url=url) from exc


def _parse_with_bs4(html_text: str) -> tuple[str, str, str, str]:
    soup = BeautifulSoup(html_text, "html.parser")

    body_node = (
        soup.select_one("#dic_area")
        or soup.select_one("#newsct_article")
        or soup.select_one("#articleBodyContents")
        or soup.select_one("#articeBody")
        or soup.select_one("article")
    )
    body_text = ""
    if body_node:
        for tag in body_node.select(
            "script, style, .end_photo_org, .vod_player_wrap, .promotion"
        ):
            tag.decompose()
        body_text = re.sub(r"\n{3,}", "\n\n", body_node.get_text("\n").strip())

    t = (
        soup.select_one("#title_area span")
        or soup.select_one("h2#title_area")
        or soup.select_one("h3#articleTitle")
        or soup.select_one("h2.media_end_head_headline")
        or soup.select_one('meta[property="og:title"]')
    )
    title = ""
    if t is not None:
        title = (t.get("content") if t.name == "meta" else t.get_text(strip=True)) or ""

    p = (
        soup.select_one(".media_end_head_top_logo img")
        or soup.select_one('meta[property="og:article:author"]')
        or soup.select_one('meta[name="twitter:creator"]')
    )
    press = ""
    if p is not None:
        press = p.get("alt") or p.get("content") or ""
        if "|" in press:
            press = press.split("|", 1)[0].strip()

    d = (
        soup.select_one(".media_end_head_info_datestamp_time")
        or soup.select_one('meta[property="article:published_time"]')
    )
    published_at = ""
    if d is not None:
        published_at = d.get("data-date-time") or d.get("content") or d.get_text(strip=True) or ""

    return title, press, published_at, body_text


def _parse_with_regex(html_text: str) -> tuple[str, str, str, str]:
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
    title = html.unescape(m.group(1)) if m else ""
    m = re.search(r'<meta property="article:published_time" content="([^"]+)"', html_text)
    published_at = m.group(1) if m else ""
    m = re.search(
        r'<div[^>]+id="(?:dic_area|newsct_article|articleBodyContents|articeBody)"[^>]*>(.*?)</div>',
        html_text,
        re.S,
    )
    body_text = html.unescape(_TAG_RE.sub("", m.group(1))).strip() if m else ""
    return title, "", published_at, body_text


def fetch_news_article(url: str, *, timeout: float = 10.0) -> NewsArticle:
    """네이버 뉴스 URL에서 본문 등을 크롤링.

    Raises:
        NewsScrapeError: 네이버 뉴스 호스트가 아니거나, 네트워크/파싱 실패 시.
    """
    if not _is_naver_news(url):
        raise NewsScrapeError(
            f"지원하지 않는 호스트입니다 (네이버 뉴스 URL만 지원): {url}",
            url=url,
        )

    html_text = _fetch(url, timeout=timeout)

    if _HAS_BS4:
        title, press, published_at, body = _parse_with_bs4(html_text)
    else:
        title, press, published_at, body = _parse_with_regex(html_text)

    if not body:
        raise NewsScrapeError("본문을 찾지 못했습니다.", url=url)

    return NewsArticle(
        url=url,
        title=title,
        press=press,
        published_at=published_at,
        body=body,
    )
