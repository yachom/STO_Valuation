"""주소 → 반경 내 아파트 세대수 조회 end-to-end 시험.

전제: real_estate/database/DB/real_estate.db 빌드 완료.
빌드: real_estate/database/db_builder/build_master_db.py

필요 env: KAKAO_API_KEY, JUSO_API_KEY

사용:
    python -m scripts.try_nearby_apartment "서울특별시 강남구 테헤란로 152"
    python -m scripts.try_nearby_apartment "..." 1000   # 반경(미터) 지정
"""

import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.repository.nearby_apartment import NearbyApartmentRepository

DB_PATH = Path("real_estate/database/DB/real_estate.db")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    missing = [k for k in ("KAKAO_API_KEY", "JUSO_API_KEY") if not os.environ.get(k)]
    if missing:
        sys.exit(f"필요한 env가 비어 있습니다: {', '.join(missing)}")
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} 가 없습니다. build_master_db로 먼저 빌드하세요.")

    args = sys.argv[1:]
    if not args:
        raw = input("주소: ")
        radius = 500
    else:
        raw = args[0]
        radius = int(args[1]) if len(args) >= 2 else 500
    if not raw:
        sys.exit("주소가 비어 있습니다.")

    resolver = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )
    location = resolver.resolve(raw)
    print("\n=== 위치 ===")
    print(location)

    repo = NearbyApartmentRepository(db_path=str(DB_PATH), radius_m=radius)
    result = repo.fetch(location)

    print(f"\n=== 반경 {radius}m 내 아파트 ===")
    for k, v in result.items():
        if isinstance(v, list):
            print(f"  {k}: {len(v)}건")
        else:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
