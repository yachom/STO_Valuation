"""네이버 검색 비(非)뉴스 vertical(blog/cafearticle/webkr/kin) → 스니펫 리스트.

news_search.py 와 평행. 같은 search family 엔드포인트
`https://openapi.naver.com/v1/search/{vertical}.json` 를 호출하되, vertical 마다
응답 필드가 조금씩 다르다(blog=bloggername/postdate, cafe=cafename, web/kin=날짜
없음). 이 모듈은 그 차이를 흡수해 공통 shape(VerticalItem)으로 정규화한다.

news 는 본문 스크랩 경로(naver_news_scraper)가 별도로 있으나, 여기 vertical 들은
스크랩 대상이 아니다 — Open API 가 주는 description(스니펫)을 그대로 문서 본문으로
쓴다(Tavily content 인라인과 동일 전략). 따라서 link 는 출처 표기용이다.

NaverSearchClient(인증·에러·로깅·게이트웨이)·_http·gateway 를 그대로 재사용한다.
로거 컨벤션: 호출 로깅은 client 가 external.naver_search 로 남긴다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from infra.external.naver_search._text import normalize_pub_date as _normalize_pub_date
from infra.external.naver_search._text import strip_html as _strip_html
from infra.external.naver_search.client import NaverSearchClient

# news 는 news_search.py 가 담당. 여기는 스크랩 불필요한 스니펫 vertical 만.
_VERTICALS = ("blog", "cafearticle", "webkr", "kin")
_API_URL = "https://openapi.naver.com/v1/search/{vertical}.json"


@dataclass(frozen=True)
class VerticalItem:
    """vertical 무관 정규화 스니펫 1건.

    description 이 곧 문서 본문(스니펫)이다. source_name 은 vertical 별 출처
    이름(blog=블로거명, cafe=카페명)이며 없으면 빈 문자열. posted_at 은 ISO 8601
    이거나 vertical 이 날짜를 안 주면 빈 문자열.
    """
    title: str
    description: str       # 스니펫 = 문서 본문 (스크랩 없음)
    link: str
    source_name: str       # bloggername / cafename / "" (web/kin)
    posted_at: str         # ISO 8601 or ""
    vertical: str          # "blog" | "cafearticle" | "webkr" | "kin"

    def to_dict(self) -> dict:
        return asdict(self)


def _source_name(vertical: str, raw: dict) -> str:
    """vertical 별 출처 이름 필드 차이 흡수."""
    if vertical == "blog":
        return raw.get("bloggername", "") or ""
    if vertical == "cafearticle":
        return raw.get("cafename", "") or ""
    return ""                                  # webkr / kin 은 출처 이름 없음


def _posted_at(vertical: str, raw: dict) -> str:
    """vertical 별 날짜 필드 차이 흡수.

    blog 은 postdate 가 YYYYMMDD(예 '20240131'), cafe 은 보통 날짜 없음,
    web/kin 도 날짜 필드가 없다. RFC822 가 아닌 YYYYMMDD 는 그대로 두면
    normalize 가 원문 유지하므로, 하이픈 ISO date 로 가볍게 변환한다.
    """
    if vertical == "blog":
        pd = (raw.get("postdate") or "").strip()
        if len(pd) == 8 and pd.isdigit():
            return f"{pd[:4]}-{pd[4:6]}-{pd[6:]}"
        return _normalize_pub_date(pd)
    return _normalize_pub_date(raw.get("pubDate", ""))   # 대개 없음 → ""


def fetch_vertical_metadata(
    client: NaverSearchClient,
    query: str,
    *,
    vertical: str,
    display: int = 100,
    start: int = 1,
    sort: str = "sim",
) -> list[VerticalItem]:
    """검색어 + vertical → 정규화 스니펫 리스트.

    Args:
        client: 인증된 NaverSearchClient (news 와 공용)
        query: 검색어
        vertical: "blog" | "cafearticle" | "webkr" | "kin"
        display: 1~100
        start: 1~1000
        sort: "sim" | "date" (webkr/kin 은 sort 미지원 → 무시될 수 있음)
    """
    if vertical not in _VERTICALS:
        raise ValueError(f"vertical 은 {_VERTICALS} 중 하나여야 합니다 (받음: {vertical!r}).")
    if not query or not query.strip():
        raise ValueError("query는 비어 있을 수 없습니다.")
    if not 1 <= display <= 100:
        raise ValueError("display는 1~100 사이여야 합니다.")
    if not 1 <= start <= 1000:
        raise ValueError("start는 1~1000 사이여야 합니다.")
    if sort not in {"date", "sim"}:
        raise ValueError("sort는 'date' 또는 'sim'이어야 합니다.")

    body = client.get(
        _API_URL.format(vertical=vertical),
        params={"query": query, "display": display, "start": start, "sort": sort},
    )

    items: list[VerticalItem] = []
    for raw in body.get("items", []):
        items.append(
            VerticalItem(
                title=_strip_html(raw.get("title", "")),
                description=_strip_html(raw.get("description", "")),
                link=raw.get("link", ""),
                source_name=_strip_html(_source_name(vertical, raw)),
                posted_at=_posted_at(vertical, raw),
                vertical=vertical,
            )
        )
    return items
