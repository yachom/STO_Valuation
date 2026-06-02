"""주소 → 상권 인구·매출 조회 end-to-end 시험.

흐름:
    1) Kakao + Juso로 주소를 LocationInfo로 정규화 (좌표 확보)
    2) CommercialAreaRepository로 상권/행정동 인구 dict 조회
    3) 매칭수준 + 상권 수 + 첫 상권의 분기별 유동/거주 인구 일부 출력

전제:
    real_estate/database/DB/commercial_area.db 가 빌드돼 있어야 한다.
    빌드 방법:
        real_estate/database/raw_data/ 에 서울시 상권분석서비스 원본
        파일들을 넣은 뒤,
        python -m real_estate.database.db_builder.build_commercial_area_db

필요 env (.env):
    KAKAO_API_KEY, JUSO_API_KEY

사용:
    python -m scripts.try_commercial_area "서울특별시 강남구 테헤란로 152"
"""

import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.repository.commercial_area import CommercialAreaRepository

DB_PATH = Path("real_estate/database/DB/commercial_area.db")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    needed = ["KAKAO_API_KEY", "JUSO_API_KEY"]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        sys.exit(f"필요한 env가 비어 있습니다: {', '.join(missing)}")

    if not DB_PATH.exists():
        sys.exit(
            f"{DB_PATH} 가 없습니다. "
            "raw_data를 채운 뒤 build_commercial_area_db로 먼저 빌드하세요."
        )

    raw = " ".join(sys.argv[1:]).strip() or input("주소: ")
    if not raw:
        sys.exit("주소가 비어 있습니다.")

    resolver = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )
    location = resolver.resolve(raw)
    print("\n=== 위치 ===")
    print(location)

    repo = CommercialAreaRepository(db_path=str(DB_PATH))
    result = repo.fetch(location)

    print(f"\n=== 매칭수준: {result['매칭수준']} (상권 {result['매칭상권수']}개) ===")
    if not result["상권목록"]:
        print("매칭된 상권이 없습니다.")
        return

    first = result["상권목록"][0]
    print(f"\n[상권 1] {first.get('상권명') or first.get('행정동명') or '-'}")

    floats = first.get("유동인구") or []
    print(f"  유동인구 분기 {len(floats)}건")
    for q in floats[:2]:
        print(f"    {q.get('기준분기')}: 총 {q.get('총')}명")

    residents = first.get("거주인구") or []
    print(f"  거주인구 분기 {len(residents)}건")
    for q in residents[:2]:
        print(f"    {q.get('기준분기')}: 총 {q.get('총')}명")


if __name__ == "__main__":
    main()
