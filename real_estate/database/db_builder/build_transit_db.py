"""build_transit_db.py — 서울 지하철역 + 버스정류소 좌표 적재.

상가 평가의 입지 접근성 axis 핵심 데이터. 평가 대상 건물 좌표 → 가장 가까운
역까지 거리 + 200m/500m 안 정류장 수는 임차 매력도와 직접 연결.

서비스:
    지하철: SearchSTNBySubwayLineService
        필드: STATION_CD/STATN_NM/LINE_NUM/XPOINT_WGS/YPOINT_WGS 등
    버스:   tbBusStopLocationXyInfo
        필드: NODE_ID/STOPS_NO/STOPS_NM/XCRD/YCRD 등

사용:
    python -m real_estate.database.db_builder.build_transit_db
        --subway --bus              # 둘 다
    python -m real_estate.database.db_builder.build_transit_db --subway
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from infra.env import load_env
from infra.external.seoul_open_data import SeoulOpenDataClient
from real_estate.database.schema import ensure_schema

SUBWAY_SVC = "SearchSTNBySubwayLineService"
BUS_SVC = "tbBusStopLocationXyInfo"
DB_PATH = _PROJECT_ROOT / "real_estate" / "database" / "DB" / "real_estate.db"

logger = logging.getLogger("builder.transit")


def _f(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def parse_subway(row: dict) -> dict | None:
    name = row.get("STATN_NM") or row.get("STATION_NM") or row.get("BLDN_NM")
    line = row.get("LINE_NUM") or row.get("ROUTE")
    lat = _f(row.get("YPOINT_WGS") or row.get("LAT"))
    lon = _f(row.get("XPOINT_WGS") or row.get("LOT") or row.get("LON"))
    if not name or lat is None or lon is None:
        return None
    station_id = f"subway:{line or '?'}:{name}"
    return {
        "station_id": station_id,
        "station_name": name,
        "line_name": str(line) if line else None,
        "operator": row.get("OPER_INSTT_NM") or row.get("OPER"),
        "latitude": lat,
        "longitude": lon,
        "daily_boardings": None,
        "raw_payload": json.dumps(row, ensure_ascii=False),
    }


def parse_bus_stop(row: dict) -> dict | None:
    stop_id = row.get("NODE_ID") or row.get("STOPS_NO") or row.get("STATIONS_ID")
    if not stop_id:
        return None
    name = row.get("STOPS_NM") or row.get("STATION_NM")
    lat = _f(row.get("YCRD") or row.get("LATITUDE") or row.get("STOPS_Y"))
    lon = _f(row.get("XCRD") or row.get("LONGITUDE") or row.get("STOPS_X"))
    if name is None or lat is None or lon is None:
        return None
    return {
        "stop_id": str(stop_id),
        "stop_name": name,
        "latitude": lat,
        "longitude": lon,
        "stop_kind": row.get("STOP_TYPE_NM") or row.get("STOPS_KIND"),
        "raw_payload": json.dumps(row, ensure_ascii=False),
    }


def build_subway(
    client: SeoulOpenDataClient,
    conn: sqlite3.Connection,
) -> int:
    inserted = 0
    cur = conn.cursor()
    for row in client.iter_rows(SUBWAY_SVC, page_size=1000):
        norm = parse_subway(row)
        if norm is None:
            continue
        result = cur.execute(
            """
            INSERT OR REPLACE INTO transit_station (
                station_id, station_name, line_name, operator,
                latitude, longitude, daily_boardings, raw_payload
            ) VALUES (
                :station_id, :station_name, :line_name, :operator,
                :latitude, :longitude, :daily_boardings, :raw_payload
            )
            """,
            norm,
        )
        if result.rowcount:
            inserted += 1
    conn.commit()
    logger.info("subway stations inserted: %d", inserted)
    return inserted


def build_bus(
    client: SeoulOpenDataClient,
    conn: sqlite3.Connection,
) -> int:
    inserted = 0
    cur = conn.cursor()
    for row in client.iter_rows(BUS_SVC, page_size=1000):
        norm = parse_bus_stop(row)
        if norm is None:
            continue
        result = cur.execute(
            """
            INSERT OR REPLACE INTO bus_stop (
                stop_id, stop_name, latitude, longitude, stop_kind, raw_payload
            ) VALUES (
                :stop_id, :stop_name, :latitude, :longitude, :stop_kind, :raw_payload
            )
            """,
            norm,
        )
        if result.rowcount:
            inserted += 1
    conn.commit()
    logger.info("bus stops inserted: %d", inserted)
    return inserted


def build(
    *,
    subway: bool = True,
    bus: bool = True,
    db_path: Path = DB_PATH,
    service_key: str | None = None,
) -> dict:
    service_key = service_key or os.environ.get("SEOUL_OPEN_DATA_KEY", "")
    if not service_key:
        raise RuntimeError("SEOUL_OPEN_DATA_KEY env가 비어 있습니다.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    client = SeoulOpenDataClient(service_key=service_key)
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        ensure_schema(conn)
        summary = {}
        if subway:
            summary["subway"] = build_subway(client, conn)
        if bus:
            summary["bus"] = build_bus(client, conn)
        return summary
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--subway", action="store_true")
    parser.add_argument("--bus", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="subway+bus (기본)")
    args = parser.parse_args()
    if not (args.subway or args.bus):
        args.all = True
    summary = build(
        subway=(args.subway or args.all),
        bus=(args.bus or args.all),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
