"""게이트웨이 + 네이버 검색/스크래퍼 동시 시험.

N개 검색어를 각각 스레드로 동시 실행. 각 스레드는
    search(display=50) → 네이버 뉴스 호스트인 link만 골라 fetch_news_article.
게이트웨이가 naver_search / naver_news_scraper 호출 빈도를 API별 min_interval로
직렬화하므로, 동시 스레드 수와 무관하게 호출 간격이 보장되어야 한다.

사용:
    python -m scripts.try_gateway_with_naver "삼성전자" "현대차" "SK하이닉스"
    python -m scripts.try_gateway_with_naver        # 기본 키워드 3개
"""

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from infra.env import load_env
from infra.external.naver_news_scraper import NewsScrapeError, fetch_news_article
from infra.external.naver_search import (
    NaverSearchClient,
    fetch_news_metadata,
    load_credentials,
)

_DEFAULT_QUERIES = ["삼성전자", "SK하이닉스", "현대차"]
_DISPLAY = 50


def _run_query(client: NaverSearchClient, query: str) -> dict:
    t0 = time.monotonic()
    metas = fetch_news_metadata(client, query, display=_DISPLAY, sort="date")
    naver_links = [m.link for m in metas if m.link and "n.news.naver.com" in m.link]

    scraped: list[tuple[str, str, str]] = []
    fails = 0
    for url in naver_links:
        try:
            article = fetch_news_article(url)
        except NewsScrapeError as exc:
            fails += 1
            logging.getLogger("scripts.try_gateway_with_naver").warning(
                "[%s] scrape fail: %s", query, exc
            )
            continue
        body_preview = article.body[:80].replace("\n", " ")
        scraped.append((url, article.title, body_preview))

    return {
        "query": query,
        "meta_count": len(metas),
        "naver_link_count": len(naver_links),
        "scraped": scraped,
        "fails": fails,
        "elapsed": time.monotonic() - t0,
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(threadName)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("infra.gateway").setLevel(logging.DEBUG)

    load_env()
    credentials = load_credentials()
    client = NaverSearchClient(credentials=credentials)

    queries = sys.argv[1:] or _DEFAULT_QUERIES
    print(f"\n=== {len(queries)}개 검색어 동시 실행 (display={_DISPLAY}) ===")

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(queries)) as ex:
        futures = [ex.submit(_run_query, client, q) for q in queries]
        results = [f.result() for f in as_completed(futures)]
    total = time.monotonic() - started

    print(f"\n=== 총 경과 {total:.2f}s ===")
    for r in results:
        print(
            f"\n[{r['query']}] {r['elapsed']:.2f}s | "
            f"검색 {r['meta_count']} / 네이버링크 {r['naver_link_count']} / "
            f"본문확보 {len(r['scraped'])} / 실패 {r['fails']}"
        )
        for url, title, body in r["scraped"][:2]:
            print(f"  · {title}")
            print(f"    {body}...")


if __name__ == "__main__":
    main()
