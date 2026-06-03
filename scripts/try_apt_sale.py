"""MOLIT 아파트 매매 실거래가 — 시군구 1개 + 1개월 샘플 적재 확인.

전제: real_estate.db 존재 (master_db 빌더 완료 후 사용 권장).
필요 env: MOLIT_REAL_TRANSACTION_KEY

사용:
    python -m scripts.try_apt_sale [LAWD_CD] [YYYYMM]
    예) python -m scripts.try_apt_sale 11680 202604   # 강남구, 2026년 4월
    인자 생략 시 11680 + 한 달 전.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from infra.env import load_env
from real_estate.database.db_builder.build_apt_sale_db import (
    SEOUL_LAWD_CDS,
    build,
)
from real_estate.repository.apt_sale import AptSaleTransactionRepository

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

    if not os.environ.get("MOLIT_REAL_TRANSACTION_KEY"):
        sys.exit("MOLIT_REAL_TRANSACTION_KEY env가 비어 있습니다.")
    if not DB_PATH.parent.exists():
        sys.exit(f"{DB_PATH.parent} 가 없습니다.")

    args = sys.argv[1:]
    lawd_cd = args[0] if len(args) >= 1 else "11680"  # 강남구
    ymd = args[1] if len(args) >= 2 else _default_recent_ymd()

    if lawd_cd not in SEOUL_LAWD_CDS:
        print(f"경고: {lawd_cd}는 Seoul LAWD_CD 표에 없는 코드입니다.")

    print(f"\n=== build (LAWD_CD={lawd_cd}, DEAL_YMD={ymd}) ===")
    summary = build(lawd_cds=[lawd_cd], deal_ymds=[ymd], db_path=DB_PATH)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n=== 시군구 최근 10건 샘플 ===")
    repo = AptSaleTransactionRepository(db_path=str(DB_PATH))
    rows = repo.fetch_recent_in_lawd(
        lawd_cd, ymd_from=ymd, ymd_to=ymd, limit=10,
    )
    for r in rows:
        print(
            f"  {r['deal_year_month']}.{r['deal_day']:02d} "
            f"{r['apt_name']} {r['area_sqm']}m² {r['floor']}F "
            f"= {r['deal_amount']:,}만원 (master_id={r.get('master_complex_id')})"
        )


if __name__ == "__main__":
    main()
