"""NTS 사업자등록 통계 표본 적재 확인.

필요 env: NTS_BUSINESS_KEY
사용:
    python -m scripts.try_nts_business [--endpoint URL] [--quarter 20254]
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.database.db_builder.build_nts_business_db import (
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
    if not os.environ.get("NTS_BUSINESS_KEY"):
        sys.exit("NTS_BUSINESS_KEY env가 비어 있습니다.")

    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--quarter", default=None)
    args = parser.parse_args()

    summary = build(endpoint=args.endpoint, quarter=args.quarter,
                    db_path=DB_PATH)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n=== 상위 폐업률 업종 (최근 분기) ===")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT region_name, industry_name,
               new_count, closed_count, surviving_count,
               CASE WHEN surviving_count > 0
                    THEN ROUND(100.0 * closed_count / surviving_count, 2)
                    ELSE NULL END AS closed_pct
        FROM business_registry_stats_quarterly
        WHERE quarter_code = (SELECT MAX(quarter_code) FROM business_registry_stats_quarterly)
          AND closed_count IS NOT NULL
        ORDER BY closed_pct DESC NULLS LAST
        LIMIT 10
        """
    ).fetchall()
    conn.close()
    for r in rows:
        print(
            f"  [{r['region_name']}] {r['industry_name']}: "
            f"신규 {r['new_count']} / 폐업 {r['closed_count']} / "
            f"생존 {r['surviving_count']} → 폐업률 {r['closed_pct']}%"
        )


if __name__ == "__main__":
    main()
