"""MOLIT 상업·업무용 매매 실거래가 — 시군구 1개 + 1개월 샘플 적재 확인.

필요 env: MOLIT_NRG_TRADE_KEY
사용:
    python -m scripts.try_commercial_sale [LAWD_CD] [YYYYMM]
    예) python -m scripts.try_commercial_sale 11680 202604  # 강남구 2026-04
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from infra.env import load_env
from real_estate.database.db_builder.build_commercial_sale_db import (
    SEOUL_LAWD_CDS,
    build,
)

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
    if not os.environ.get("MOLIT_NRG_TRADE_KEY"):
        sys.exit("MOLIT_NRG_TRADE_KEY env가 비어 있습니다.")

    args = sys.argv[1:]
    lawd_cd = args[0] if len(args) >= 1 else "11680"
    ymd = args[1] if len(args) >= 2 else _default_recent_ymd()
    if lawd_cd not in SEOUL_LAWD_CDS:
        print(f"경고: {lawd_cd}는 Seoul LAWD_CD 표에 없는 코드입니다.")

    print(f"\n=== build (LAWD_CD={lawd_cd}, DEAL_YMD={ymd}) ===")
    summary = build(lawd_cds=[lawd_cd], deal_ymds=[ymd], db_path=DB_PATH)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n=== 최근 10건 샘플 ===")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT deal_year_month, deal_day, building_name, property_type, "
        "floor_total_sqm, floor, deal_amount, legal_dong, jibun "
        "FROM commercial_sale_transaction "
        "WHERE lawd_cd=? AND deal_year_month=? "
        "ORDER BY deal_day DESC LIMIT 10",
        (lawd_cd, ymd),
    ).fetchall()
    conn.close()
    for r in rows:
        day_str = f"{r['deal_day']:02d}" if r['deal_day'] else "??"
        print(
            f"  {r['deal_year_month']}.{day_str} "
            f"{r['legal_dong']} {r['jibun']} | "
            f"{r['building_name'] or '(이름미상)'} ({r['property_type'] or '?'}) "
            f"{r['floor_total_sqm']}㎡ {r['floor']}F = "
            f"{r['deal_amount']:,}만원"
        )


if __name__ == "__main__":
    main()
