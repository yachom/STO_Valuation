"""키워드 → 네이버 뉴스 검색 → 본문 스크랩 한 번에 시험해보는 스크립트.

검색 결과의 link(네이버 뉴스 URL)만 골라 순회하며 본문을 긁는다.
네이버 뉴스 호스트가 아닌 결과는 스킵, 스크랩 실패한 건도 스킵하고 진행.

사용:
    python -m scripts.try_naver_news "삼성전자"
    python -m scripts.try_naver_news "삼성전자" 5         # display 개수 지정
    python -m scripts.try_naver_news                       # 인자 없으면 stdin
"""

import logging
import sys

from infra.env import load_env
from infra.external.naver_news_scraper import (
    NewsScrapeError,
    fetch_news_article,
)
from infra.external.naver_search import (
    NaverSearchClient,
    fetch_news_metadata,
    load_credentials,
)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    load_env()

    args = sys.argv[1:]
    if args:
        query = args[0]
        display = int(args[1]) if len(args) >= 2 else 10
    else:
        query = input("검색어: ").strip()
        display = 10

    if not query:
        sys.exit("검색어가 비어 있습니다.")

    credentials = load_credentials()
    client = NaverSearchClient(credentials=credentials)

    metadatas = fetch_news_metadata(client, query, display=display, sort="date")
    print(f"\n=== 검색 결과 {len(metadatas)}건 ===")

    articles = []
    for idx, meta in enumerate(metadatas, start=1):
        print(f"\n[{idx}] {meta.title}")
        print(f"    pub_date     = {meta.pub_date}")
        print(f"    link         = {meta.link or '(없음)'}")
        print(f"    originallink = {meta.originallink}")

        if not meta.link:
            print("    -> 네이버 뉴스 link 없음, 스킵.")
            continue
        try:
            article = fetch_news_article(meta.link)
        except NewsScrapeError as exc:
            print(f"    -> 스크랩 실패: {exc}")
            continue

        articles.append(article)
        body_preview = article.body[:120].replace("\n", " ")
        print(f"    press        = {article.press}")
        print(f"    published_at = {article.published_at}")
        print(f"    body[:120]   = {body_preview}...")

    print(f"\n=== 본문 확보: {len(articles)}건 / 검색 {len(metadatas)}건 ===")


if __name__ == "__main__":
    main()
