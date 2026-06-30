"""주소 한 줄 입력 → 전 구간 외부 호출 점검 시나리오.

흐름:
    1) Kakao + Juso로 주소를 LocationInfo로 정규화
    2) 공공데이터포털 건축HUB로 건축물대장(표제부/층별/지역지구) 조회
    3) 위 결과에서 흥미로운 키워드 후보(건물명·주용도·법정동·지역지구 등) 추출
    4) 랜덤으로 하나 골라 Naver 뉴스 검색
    5) 검색 결과 중 네이버 뉴스 호스트인 첫 건을 스크랩해 본문 일부 출력

검증 대상:
    - infra.env 로더
    - real_estate.location (Kakao + Juso)
    - real_estate.external.public_data_portal + real_estate.external.building_hub
    - infra.gateway (Naver 검색·스크랩 + 공공데이터포털 모두 통과)
    - infra.external.naver_search, infra.external.naver_news_scraper

필요 env (.env):
    KAKAO_API_KEY, JUSO_API_KEY,
    BUILDING_HUB_API_KEY,
    NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

사용:
    python -m scripts.try_end_to_end "서울특별시 강남구 테헤란로 152"
    python -m scripts.try_end_to_end                            # 인자 없으면 stdin
"""

import logging
import os
import random
import sys

from infra.env import load_env
from infra.external.naver_news_scraper import NewsScrapeError, fetch_news_article
from infra.external.naver_search import (
    NaverSearchClient,
    fetch_news_metadata,
    load_credentials,
)
from real_estate.external.public_data_portal import (
    PublicDataPortalClient,
    PublicDataPortalError,
)
from real_estate.external.building_hub import API_NAME, BuildingRegisterRepository
from real_estate.location import LocationResolver


def _collect_keywords(location, building) -> list[str]:
    """LocationInfo + 건축물대장 결과에서 검색해볼 만한 키워드 후보를 모은다."""
    candidates: list[str] = []

    title = building.get("표제부") or {}
    bld_name = title.get("건물명")
    if bld_name:
        candidates.append(bld_name)
    main_use = title.get("주용도")
    if main_use:
        candidates.append(main_use)

    zones = building.get("지역지구구역") or []
    for z in zones:
        nm = z.get("명칭")
        if nm:
            candidates.append(nm)

    if location.bname:
        candidates.append(f"{location.sigungu or ''} {location.bname}".strip())
    if location.sigungu:
        candidates.append(location.sigungu)

    seen = set()
    deduped = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    needed = ["KAKAO_API_KEY", "JUSO_API_KEY", "BUILDING_HUB_API_KEY",
              "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        sys.exit(f"필요한 env가 비어 있습니다: {', '.join(missing)}")

    raw = " ".join(sys.argv[1:]).strip() or input("주소: ")
    if not raw:
        sys.exit("주소가 비어 있습니다.")

    # 1) 주소 정규화
    print("\n=== 1) 주소 정규화 ===")
    resolver = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )
    location = resolver.resolve(raw)
    print(location)

    # 2) 건축물대장 조회
    print("\n=== 2) 건축물대장 ===")
    pdp_client = PublicDataPortalClient(
        service_key=os.environ["BUILDING_HUB_API_KEY"],
        api_name=API_NAME,
    )
    repo = BuildingRegisterRepository(client=pdp_client)
    try:
        building = repo.fetch(location)
    except PublicDataPortalError as exc:
        print(f"건축HUB 호출 실패: {exc}")
        building = {"표제부": {}, "층별개요": {}, "지역지구구역": []}
    except ValueError as exc:
        print(f"위치 정보 부족: {exc}")
        building = {"표제부": {}, "층별개요": {}, "지역지구구역": []}

    title = building["표제부"]
    for k in ("건물명", "주용도", "구조", "연면적", "지상층수", "사용승인일"):
        print(f"  {k:>10}: {title.get(k)}")
    floors = building["층별개요"]
    print(f"  층별개요: {len(floors)}개 층")
    zones = building["지역지구구역"]
    print(f"  지역지구: {len(zones)}건")

    # 3) 키워드 후보
    keywords = _collect_keywords(location, building)
    print("\n=== 3) 키워드 후보 ===")
    for k in keywords:
        print(f"  · {k}")
    if not keywords:
        sys.exit("검색 키워드를 추출할 수 없습니다.")
    chosen = random.choice(keywords)
    print(f"\n→ 랜덤 선택: {chosen!r}")

    # 4) Naver 검색
    print("\n=== 4) 네이버 뉴스 검색 ===")
    naver_client = NaverSearchClient(credentials=load_credentials())
    metas = fetch_news_metadata(naver_client, chosen, display=10, sort="date")
    print(f"검색 결과 {len(metas)}건")
    for i, m in enumerate(metas[:5], start=1):
        print(f"  [{i}] {m.title}")
        print(f"      link: {m.link}")

    # 5) 첫 번째 네이버 호스트 결과 스크랩
    print("\n=== 5) 네이버 본문 스크랩 ===")
    naver_links = [m for m in metas if m.link and "n.news.naver.com" in m.link]
    if not naver_links:
        print("네이버 뉴스 호스트의 결과가 없습니다.")
        return
    target = naver_links[0]
    try:
        article = fetch_news_article(target.link)
    except NewsScrapeError as exc:
        print(f"스크랩 실패: {exc}")
        return
    print(f"제목: {article.title}")
    print(f"언론: {article.press}")
    print(f"발행: {article.published_at}")
    body_preview = article.body[:200].replace("\n", " ")
    print(f"본문[:200]: {body_preview}...")


if __name__ == "__main__":
    main()
