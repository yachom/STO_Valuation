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

# 1611000은 deprecated. 1613000이 현행 endpoint.
ENDPOINT = (
    "https://apis.data.go.kr/1613000/RTMSDataSvcNrgTrade/getRTMSDataSvcNrgTrade"
)
API_NAME = "molit_nrg_trade"
DB_PATH = _PROJECT_ROOT / "real_estate" / "database" / "DB" / "real_estate.db"
PAGE_SIZE = 1000

logger = logging.getLogger("builder.commercial_sale")


def _g(d, *keys):
    """fallback: 한국어 키 → 영문 키 순서로 try."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return ""


def parse_item(raw: dict, *, lawd_cd: str) -> dict:
    """MOLIT NRG XML item → DB row.

    현행 1613000 endpoint는 **영문 필드명**으로 응답:
        dealAmount, dealYear, dealMonth, dealDay, umdNm(법정동), bldgNm(건물명),
        buildingUse(건물주용도), buildingAr(전용면적/대지권면적), floor, buildYear,
        jibun, roadNm, sggNm(시군구명), sggCd, umdCd(법정동읍면동), buyerGbn,
        dealingGbn, cdealType, cdealDay.
    한국어 키도 fallback으로 (이전 1611000 호환).
    """
    amt_str = str(_g(raw, "거래금액", "dealAmount")).replace(",", "").strip()
    deal_amount = int(amt_str) if amt_str.isdigit() else None

    year = str(_g(raw, "년", "dealYear")).strip()
    month_raw = str(_g(raw, "월", "dealMonth")).strip()
    month = month_raw.zfill(2) if month_raw else ""
    day_str = str(_g(raw, "일", "dealDay")).strip()
    deal_day = int(day_str) if day_str.isdigit() else None
    deal_year_month = f"{year}{month}" if year and month else ""

    area_str = str(_g(raw, "대지권면적", "건물면적", "buildingAr")).strip()
    try:
        floor_area = float(area_str) if area_str else None
    except ValueError:
        floor_area = None

    floor_str = str(_g(raw, "층", "floor")).strip()
    floor = int(floor_str) if floor_str.lstrip("-").isdigit() else None

    by_str = str(_g(raw, "건축년도", "buildYear")).strip()
    build_year = int(by_str) if by_str.isdigit() else None

    sub_code = str(_g(raw, "법정동읍면동코드", "umdCd")).strip()
    bcode = f"{lawd_cd}{sub_code}" if sub_code and len(sub_code) == 5 else None

    cancel_flag = str(_g(raw, "해제여부", "cdealType")).strip().upper()
    is_canceled = 1 if cancel_flag == "O" else 0
    cancel_date = str(_g(raw, "해제사유발생일", "cdealDay")).strip() or None

    return {
        "deal_year_month": deal_year_month,
        "deal_day": deal_day,
        "deal_amount": deal_amount,
        "lawd_cd": lawd_cd,
        "legal_dong": str(_g(raw, "법정동", "umdNm")).strip() or None,
        "bcode": bcode,
        "jibun": str(_g(raw, "지번", "jibun")).strip() or None,
        "road_name": str(_g(raw, "도로명", "roadNm")).strip() or None,
        "building_name": str(_g(raw, "건물명", "bldgNm")).strip() or None,
        "property_type": (
            str(_g(raw, "건물주용도", "buildingUse", "유형", "buildingType", "구분")).strip()
            or None
        ),
        "floor_total_sqm": floor_area,
        "floor": floor,
        "build_year": build_year,
        "bd_mgt_sn": None,
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
