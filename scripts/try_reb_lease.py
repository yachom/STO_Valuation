"""REB 상업용부동산 임대동향 표본 적재 확인.

필요 env: REB_LEASE_MARKET_KEY
사용:
    python -m scripts.try_reb_lease [--endpoint URL] [--quarter 20254]
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.database.db_builder.build_reb_lease_market_db import (
    DEFAULT_ENDPOINT,
    build,
)

DB_PATH = Path("real_estate/database/DB/real_estate.db")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()
    if not os.environ.get("REB_LEASE_MARKET_KEY"):
        sys.exit("REB_LEASE_MARKET_KEY env가 비어 있습니다.")

    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--quarter", default=None)
    args = parser.parse_args()

    summary = build(endpoint=args.endpoint, quarter=args.quarter,
                    db_path=DB_PATH)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n=== 적재된 상위 10건 ===")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sigungu_name, quarter_code, building_type, "
        "vacancy_rate, rent_per_sqm, yield_rate "
        "FROM commercial_lease_market_quarterly "
        "ORDER BY quarter_code DESC, sigungu_cd LIMIT 10"
    ).fetchall()
    conn.close()
    for r in rows:
        print(
            f"  {r['sigungu_name']} | {r['quarter_code']} {r['building_type']}: "
            f"공실 {r['vacancy_rate']}% / 임대료 {r['rent_per_sqm']}원/㎡ / "
            f"수익률 {r['yield_rate']}%"
        )


if __name__ == "__main__":
    main()
