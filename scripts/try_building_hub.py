"""주소 → LocationInfo → 건축HUB 건축물대장 조회 end-to-end 시험.

사용:
    python -m scripts.try_building_hub "서울특별시 강남구 테헤란로 152"
    python -m scripts.try_building_hub                      # 인자 없으면 stdin

필요 env (.env 또는 export):
    KAKAO_API_KEY          — 주소 검색·좌표→행정구역
    JUSO_API_KEY           — 건물관리번호 + 본번/부번
    BUILDING_HUB_API_KEY   — 건축HUB 건축물대장정보 서비스 키
"""

import logging
import os
import sys

from infra.env import load_env
from real_estate.external.public_data_portal import (
    PublicDataPortalClient,
    PublicDataPortalError,
)
from real_estate.external.building_hub import API_NAME, BuildingRegisterRepository
from real_estate.location import LocationResolver


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    kakao = os.environ.get("KAKAO_API_KEY")
    juso = os.environ.get("JUSO_API_KEY")
    bhub = os.environ.get("BUILDING_HUB_API_KEY")
    missing = [k for k, v in {
        "KAKAO_API_KEY": kakao,
        "JUSO_API_KEY": juso,
        "BUILDING_HUB_API_KEY": bhub,
    }.items() if not v]
    if missing:
        sys.exit(f"필요한 env가 비어 있습니다: {', '.join(missing)}")

    raw = " ".join(sys.argv[1:]).strip() or input("주소: ")
    if not raw:
        sys.exit("주소가 비어 있습니다.")

    resolver = LocationResolver(kakao_api_key=kakao, juso_api_key=juso)
    location = resolver.resolve(raw)
    print("\n=== 위치 정규화 ===")
    print(location)

    client = PublicDataPortalClient(service_key=bhub, api_name=API_NAME)
    repo = BuildingRegisterRepository(client=client)

    try:
        facts = repo.fetch(location)
    except PublicDataPortalError as exc:
        sys.exit(f"건축HUB 호출 실패: {exc}")

    print("\n=== 표제부 (일부) ===")
    title = facts["표제부"]
    for k in ("건물명", "주용도", "구조", "연면적", "지상층수", "지하층수",
              "사용승인일", "에너지효율등급"):
        print(f"  {k:>12}: {title.get(k)}")

    floors = facts["층별개요"]
    print(f"\n=== 층별개요: {len(floors)}개 층 ===")
    for floor_name, entries in floors.items():
        purps = ", ".join(e.get("주용도") or "-" for e in entries)
        print(f"  {floor_name}: {purps}")

    zones = facts["지역지구구역"]
    print(f"\n=== 지역지구구역: {len(zones)}건 ===")
    for z in zones:
        print(f"  · {z.get('구분')} / {z.get('명칭')} (대표={z.get('대표여부')})")


if __name__ == "__main__":
    main()
