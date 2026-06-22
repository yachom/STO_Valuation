"""ddgs(DuckDuckGo 메타서치) 웹검색/뉴스/본문추출 도구를 시험하는 스크립트.

Tavily와 달리 키가 필요 없다. text→스니펫, news→뉴스, 그리고 첫 결과는 extract로
본문 전문까지 가져와 본다.

사용:
    python -m scripts.try_ddgs "강남역 상권"
    python -m scripts.try_ddgs "강남역 상권" news        # 뉴스 채널
    python -m scripts.try_ddgs "강남역 상권" text 5       # max_results
    python -m scripts.try_ddgs                            # 인자 없으면 stdin
"""

import logging
import sys

from infra.env import load_env
from infra.tool.ddgs import DdgsExtractTool, DdgsSearchTool


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    args = sys.argv[1:]
    if args:
        query = args[0]
        topic = args[1] if len(args) >= 2 else "text"
        max_results = int(args[2]) if len(args) >= 3 else 6
    else:
        query = input("검색어: ").strip()
        topic, max_results = "text", 6
    if not query:
        sys.exit("검색어가 비어 있습니다.")

    found = DdgsSearchTool()(query=query, topic=topic, max_results=max_results)
    print(f"\n=== ddgs.{topic} '{query}' → {found['n_results']}건 ===")
    for idx, it in enumerate(found["items"], start=1):
        content = (it["content"] or "").replace("\n", " ")
        print(f"\n[{idx}] {it['title']}")
        print(f"    link    = {it['link']}")
        print(f"    source  = {it['source']}   pub_date = {it['pub_date']}")
        print(f"    snippet = {content[:120]}...")

    # 첫 결과 본문 추출 시연 (스니펫 → 전문)
    if found["items"]:
        url = found["items"][0]["link"]
        print(f"\n=== extract 첫 결과 본문: {url} ===")
        ext = DdgsExtractTool()(url=url)
        if ext["ok"]:
            body = ext["content"].replace("\n", " ")
            print(f"본문[:300] = {body[:300]}...  (총 {len(ext['content'])}자)")
        else:
            print(f"추출 실패: {ext.get('error')}")


if __name__ == "__main__":
    main()
