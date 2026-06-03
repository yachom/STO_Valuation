"""MOLIT 아파트 전월세 실거래가 — 시군구 1개 + 1개월 샘플 적재 + 전월세 비율 출력.

필요 env: MOLIT_RENT_KEY
사용:
    python -m scripts.try_apt_rent [LAWD_CD] [YYYYMM]
"""

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from infra.env import load_env
from real_estate.database.db_builder.build_apt_rent_db import (
    SEOUL_LAWD_CDS,
    build,
)
from real_estate.repository.apt_rent import AptRentTransactionRepository

DB_PATH = Path("real_estate/database/DB/real_estate.db")


def _default_recent_ymd() -> str:
    today = date.today()
    year = today.year
    month = today.month - 1 or 12
    if today.month == 1:
        year -= 1
    return f"{year:04d}{month:02d}"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    if not os.environ.get("MOLIT_RENT_KEY"):
        sys.exit("MOLIT_RENT_KEY env가 비어 있습니다.")

    args = sys.argv[1:]
    lawd_cd = args[0] if len(args) >= 1 else "11680"  # 강남구
    ymd = args[1] if len(args) >= 2 else _default_recent_ymd()
    if lawd_cd not in SEOUL_LAWD_CDS:
        print(f"경고: {lawd_cd}는 Seoul LAWD_CD 표에 없는 코드입니다.")

    print(f"\n=== build (LAWD_CD={lawd_cd}, DEAL_YMD={ymd}) ===")
    summary = build(lawd_cds=[lawd_cd], deal_ymds=[ymd], db_path=DB_PATH)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n=== 전월세 비율 (시군구 기준) ===")
    repo = AptRentTransactionRepository(db_path=str(DB_PATH))
    ratio = repo.jeonse_wolse_ratio(lawd_cd=lawd_cd, ymd_from=ymd, ymd_to=ymd)
    print(json.dumps(ratio, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
