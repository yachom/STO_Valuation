"""build_commercial_sale_db.py — MOLIT 상업·업무용 부동산 매매 → real_estate.db.

기존 `real_estate/external/molit_rtms/`의 `MolitRtmsClient`가 동일한 XML
envelope/resultCode를 쓰므로 그대로 재사용한다. endpoint URL과 응답 필드명만
다르다. 본 빌더는 `commercial_sale_transaction` 테이블에 적재.

평가 대상이 상가 건물이라는 점을 고려해 단지명 fuzzy 매칭은 하지 않는다.
대신 `building_name`(free text)과 `jibun`을 그대로 보존해, 추후 retriever가
사용자가 지정한 건물의 `bd_mgt_sn` 또는 좌표로부터 인접 거래를 찾을 때
활용한다.

사용:
    python -m real_estate.database.db_builder.build_commercial_sale_db
        --lawd 11680 11650 11710 --ymd 202604

기본: 서울 25개 자치구 × 최근 1개월.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from infra.env import load_env
from real_estate.database.db_builder.build_apt_sale_db import SEOUL_LAWD_CDS
from real_estate.database.schema import ensure_schema
from real_estate.external.molit_rtms import MolitRtmsClient

ENDPOINT = (
    "https://apis.data.go.kr/1611000/nrg/RTMSDataSvcNrgTrade/getRTMSDataSvcNrgTrade"
)
API_NAME = "molit_nrg_trade"
DB_PATH = _PROJECT_ROOT / "real_estate" / "database" / "DB" / "real_estate.db"
PAGE_SIZE = 1000

logger = logging.getLogger("builder.commercial_sale")


def parse_item(raw: dict, *, lawd_cd: str) -> dict:
    """MOLIT NRG XML item → DB row.

    공식 사용 필드(확인 가능 범위):
        거래금액, 거래유형, 건물명, 건물주용도, 건축년도,
        구분, 년/월/일, 대지권면적, 도로명, 법정동,
        법정동본번코드/부번코드/읍면동코드/지번코드,
        시군구코드(=LAWD_CD echo), 유형, 지번,
        지역코드, 층, 해제사유발생일, 해제여부.
    """
    amt_str = raw.get("거래금액", "").replace(",", "").strip()
    deal_amount = int(amt_str) if amt_str.isdigit() else None

    year = raw.get("년", "").strip()
    month = raw.get("월", "").strip().zfill(2)
    day_str = raw.get("일", "").strip()
    deal_day = int(day_str) if day_str.isdigit() else None
    deal_year_month = f"{year}{month}" if year and month else ""

    area_str = raw.get("대지권면적", "") or raw.get("건물면적", "")
    try:
        floor_area = float(area_str.strip()) if area_str.strip() else None
    except ValueError:
        floor_area = None

    floor_str = raw.get("층", "").strip()
    floor = int(floor_str) if floor_str.lstrip("-").isdigit() else None

    by_str = raw.get("건축년도", "").strip()
    build_year = int(by_str) if by_str.isdigit() else None

    sub_code = raw.get("법정동읍면동코드", "").strip()
    bcode = f"{lawd_cd}{sub_code}" if sub_code and len(sub_code) == 5 else None

    is_canceled = 1 if raw.get("해제여부", "").strip().upper() == "O" else 0
    cancel_date = raw.get("해제사유발생일", "").strip() or None

    return {
        "deal_year_month": deal_year_month,
        "deal_day": deal_day,
        "deal_amount": deal_amount,
        "lawd_cd": lawd_cd,
        "legal_dong": raw.get("법정동", "").strip() or None,
        "bcode": bcode,
        "jibun": raw.get("지번", "").strip() or None,
        "road_name": raw.get("도로명", "").strip() or None,
        "building_name": raw.get("건물명", "").strip() or None,
        "property_type": (
            raw.get("건물주용도", "").strip()
            or raw.get("유형", "").strip()
            or raw.get("구분", "").strip()
            or None
        ),
        "floor_total_sqm": floor_area,
        "floor": floor,
        "build_year": build_year,
        "bd_mgt_sn": None,  # NRG는 직접 제공하지 않음. 추후 JUSO 보강 빌더로.
        "is_canceled": is_canceled,
        "cancel_date": cancel_date,
        "raw_payload": json.dumps(raw, ensure_ascii=False),
    }


def build(
    *,
    lawd_cds: list[str],
    deal_ymds: list[str],
    db_path: Path = DB_PATH,
    service_key: str | None = None,
) -> dict:
    service_key = service_key or os.environ.get("MOLIT_NRG_TRADE_KEY", "")
    if not service_key:
        raise RuntimeError("MOLIT_NRG_TRADE_KEY env가 비어 있습니다.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        ensure_schema(conn)
        client = MolitRtmsClient(service_key=service_key, api_name=API_NAME)

        total_fetched = 0
        total_inserted = 0
        cur = conn.cursor()
        for lawd_cd in lawd_cds:
            for ymd in deal_ymds:
                fetched = 0
                for raw in client.iter_items(
                    ENDPOINT,
                    lawd_cd=lawd_cd,
                    deal_ymd=ymd,
                    page_size=PAGE_SIZE,
                ):
                    fetched += 1
                    row = parse_item(raw, lawd_cd=lawd_cd)
                    result = cur.execute(
                        """
                        INSERT OR IGNORE INTO commercial_sale_transaction (
                            deal_year_month, deal_day, deal_amount,
                            lawd_cd, legal_dong, bcode, jibun, road_name,
                            building_name, property_type, floor_total_sqm,
                            floor, build_year, bd_mgt_sn,
                            is_canceled, cancel_date, raw_payload
                        ) VALUES (
                            :deal_year_month, :deal_day, :deal_amount,
                            :lawd_cd, :legal_dong, :bcode, :jibun, :road_name,
                            :building_name, :property_type, :floor_total_sqm,
                            :floor, :build_year, :bd_mgt_sn,
                            :is_canceled, :cancel_date, :raw_payload
                        )
                        """,
                        row,
                    )
                    if result.rowcount:
                        total_inserted += 1
                conn.commit()
                total_fetched += fetched
                logger.info("LAWD_CD %s DEAL_YMD %s: fetched=%d",
                            lawd_cd, ymd, fetched)
        return {
            "lawd_cds": len(lawd_cds),
            "deal_ymds": len(deal_ymds),
            "total_fetched": total_fetched,
            "total_inserted": total_inserted,
        }
    finally:
        conn.close()


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--lawd", nargs="+", default=SEOUL_LAWD_CDS)
    parser.add_argument("--ymd", nargs="+", default=[_default_recent_ymd()])
    args = parser.parse_args()
    summary = build(lawd_cds=args.lawd, deal_ymds=args.ymd)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
