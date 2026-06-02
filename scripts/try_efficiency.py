"""주소 → 에너지효율등급 인증 조회 end-to-end 시험.

흐름:
    1) Kakao + Juso로 LocationInfo 정규화 (jibun_address 확보)
    2) EnergyEfficiencyGradeRepository로 efficiency.db 조회

전제:
    real_estate/database/DB/efficiency.db 가 빌드돼 있어야 한다.
    빌드: real_estate/database/db_builder/efficiency_pipeline.py 참조.

필요 env: KAKAO_API_KEY, JUSO_API_KEY

사용:
    python -m scripts.try_efficiency "서울특별시 강남구 테헤란로 152"
"""

import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.repository.efficiency import EnergyEfficiencyGradeRepository

DB_PATH = Path("real_estate/database/DB/building_certifications.db")


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
        sys.exit(
            f"{DB_PATH} 가 없습니다. "
            "real_estate/database/db_builder/efficiency_pipeline.py로 먼저 빌드하세요."
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

    repo = EnergyEfficiencyGradeRepository(db_path=str(DB_PATH))
    result = repo.fetch(location)

    print("\n=== 에너지효율등급 인증 ===")
    for k in ("energy_grade", "primary_energy_req", "energy_reduce_rate",
              "cert_type", "cert_date", "building_name", "building_category",
              "bdmgt_sn"):
        print(f"  {k:>20}: {result.get(k)}")


if __name__ == "__main__":
    main()
