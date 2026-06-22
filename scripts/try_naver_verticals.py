"""키워드 → 네이버 비뉴스 vertical(blog/cafearticle/webkr/kin) 스니펫 시험.

뉴스 외 채널을 묶어 한 번에 호출해본다. 각 vertical 은 본문 스크랩 없이
description(스니펫)을 그대로 본문으로 쓰므로, 여기서도 스니펫만 출력한다.
NAVER_CLIENT_ID/SECRET 가 .env 에 있어야 라이브 호출이 된다.

사용:
    python -m scripts.try_naver_verticals "강남역 상권"
    python -m scripts.try_naver_verticals "강남역 상권" 5          # display 개수
    python -m scripts.try_naver_verticals "강남역 상권" 5 blog,kin  # vertical 지정
    python -m scripts.try_naver_verticals                          # 인자 없으면 stdin

툴 래퍼(infra.tool.naver.NaverVerticalSearchTool) 경로도 마지막에 한 번 호출해
trace 가 정상 배선되는지 확인한다.
"""

import logging
import sys

from infra.env import load_env
from infra.external.naver_search import (
    NaverSearchClient,
    fetch_vertical_metadata,
    load_credentials,
)
from infra.tool.naver import NaverVerticalSearchTool

_DEFAULT_VERTICALS = ("blog", "cafearticle", "webkr", "kin")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    args = sys.argv[1:]
    if args:
        query = args[0]
        display = int(args[1]) if len(args) >= 2 else 5
        verticals = (
            tuple(v.strip() for v in args[2].split(",") if v.strip())
            if len(args) >= 3 else _DEFAULT_VERTICALS
        )
    else:
        query = input("검색어: ").strip()
        display = 5
        verticals = _DEFAULT_VERTICALS

    if not query:
        sys.exit("검색어가 비어 있습니다.")

    credentials = load_credentials()
    client = NaverSearchClient(credentials=credentials)

    for vertical in verticals:
        print(f"\n=== [{vertical}] '{query}' ===")
        try:
            items = fetch_vertical_metadata(
                client, query, vertical=vertical, display=display, sort="sim")
        except Exception as exc:                      # 미지원 sort/일시 오류 → 다음 vertical
            print(f"    -> 호출 실패: {type(exc).__name__}: {exc}")
            continue
        print(f"    {len(items)}건")
        for idx, it in enumerate(items, start=1):
            snippet = (it.description or "")[:120].replace("\n", " ")
            print(f"\n    [{idx}] {it.title}")
            if it.source_name:
                print(f"        source_name = {it.source_name}")
            if it.posted_at:
                print(f"        posted_at   = {it.posted_at}")
            print(f"        link        = {it.link or '(없음)'}")
            print(f"        snippet     = {snippet}...")

    # 툴 래퍼 경로 한 번 (trace 배선 sanity).
    print(f"\n=== 툴 래퍼(NaverVerticalSearchTool, blog) ===")
    tool = NaverVerticalSearchTool()
    out = tool(query=query, vertical="blog", display=display, sort="sim")
    print(f"    n_results = {out['n_results']} (vertical={out['vertical']})")


if __name__ == "__main__":
    main()
