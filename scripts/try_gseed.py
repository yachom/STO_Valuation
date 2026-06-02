"""주소 → G-SEED 녹색건축 인증 조회 end-to-end 시험.

전제: real_estate/database/DB/gseed.db 빌드 완료.
필요 env: KAKAO_API_KEY, JUSO_API_KEY

사용:
    python -m scripts.try_gseed "서울특별시 강남구 테헤란로 152"
"""

import logging
import os
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.repository.gseed import GseedCertificationRepository

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
            "real_estate/database/db_builder/gseed_pipeline.py로 먼저 빌드하세요."
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

    repo = GseedCertificationRepository(db_path=str(DB_PATH))
    result = repo.fetch(location)

    print("\n=== G-SEED 녹색건축 인증 ===")
    for k, v in result.items():
        print(f"  {k:>20}: {v}")


if __name__ == "__main__":
    main()
