"""서울 지하철·버스 좌표 적재 + 표본 출력.

필요 env: SEOUL_OPEN_DATA_KEY
사용:
    python -m scripts.try_transit         # 둘 다
    python -m scripts.try_transit --subway
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.database.db_builder.build_transit_db import build

DB_PATH = Path("real_estate/database/DB/real_estate.db")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()
    if not os.environ.get("SEOUL_OPEN_DATA_KEY"):
        sys.exit("SEOUL_OPEN_DATA_KEY env가 비어 있습니다.")

    parser = argparse.ArgumentParser()
    parser.add_argument("--subway", action="store_true")
    parser.add_argument("--bus", action="store_true")
    args = parser.parse_args()
    if not (args.subway or args.bus):
        args.subway = args.bus = True

    summary = build(
        subway=args.subway, bus=args.bus, db_path=DB_PATH,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    if args.subway:
        print("\n=== 지하철역 표본 5건 ===")
        for r in conn.execute(
            "SELECT station_name, line_name, latitude, longitude "
            "FROM transit_station LIMIT 5"
        ):
            print(f"  {r['station_name']} ({r['line_name']}): "
                  f"({r['latitude']}, {r['longitude']})")
    if args.bus:
        print("\n=== 버스정류소 표본 5건 ===")
        for r in conn.execute(
            "SELECT stop_name, latitude, longitude FROM bus_stop LIMIT 5"
        ):
            print(f"  {r['stop_name']}: ({r['latitude']}, {r['longitude']})")
    conn.close()


if __name__ == "__main__":
    main()
