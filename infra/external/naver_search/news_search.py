"""네이버 뉴스 검색 API → 메타데이터 리스트."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from infra.external.naver_search._text import normalize_pub_date as _normalize_pub_date
from infra.external.naver_search._text import strip_html as _strip_html
from infra.external.naver_search.client import NaverSearchClient

_NEWS_API_URL = "https://openapi.naver.com/v1/search/news.json"


@dataclass(frozen=True)
class NewsMetadata:
    title: str
    description: str
    link: str            # 네이버 뉴스 URL (없을 수 있음)
    originallink: str    # 원문 언론사 URL
    pub_date: str        # ISO 8601

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_news_metadata(
    client: NaverSearchClient,
    query: str,
    *,
    display: int = 100,
    start: int = 1,
    sort: str = "date",
) -> list[NewsMetadata]:
    """검색어로 네이버 뉴스 메타데이터 리스트를 반환.

    Args:
        client: 인증된 NaverSearchClient
        query: 검색어
        display: 1~100
        start: 1~1000
        sort: "date" | "sim"
    """
    if not query or not query.strip():
        raise ValueError("query는 비어 있을 수 없습니다.")
    if not 1 <= display <= 100:
        raise ValueError("display는 1~100 사이여야 합니다.")
    if not 1 <= start <= 1000:
        raise ValueError("start는 1~1000 사이여야 합니다.")
    if sort not in {"date", "sim"}:
        raise ValueError("sort는 'date' 또는 'sim'이어야 합니다.")

    body = client.get(
        _NEWS_API_URL,
        params={"query": query, "display": display, "start": start, "sort": sort},
    )

    items = []
    for raw in body.get("items", []):
        items.append(
            NewsMetadata(
                title=_strip_html(raw.get("title", "")),
                description=_strip_html(raw.get("description", "")),
                link=raw.get("link", ""),
                originallink=raw.get("originallink", ""),
                pub_date=_normalize_pub_date(raw.get("pubDate", "")),
            )
        )
    return items
