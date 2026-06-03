"""build_apt_rent_db.py — MOLIT 아파트 전월세 실거래가 → real_estate.db 적재.

국토교통부 RTMS `getRTMSDataSvcAptRent`로부터 시군구(LAWD_CD) × 거래연월(DEAL_YMD)
조합별 전월세 거래 내역을 받아 `apt_rent_transaction` 테이블에 적재한다.

매매(`build_apt_sale_db`)와 동일한 client(`MolitRtmsClient`) + 동일한 LAWD_CD/YMD
오케스트레이션을 공유한다. 차이는 endpoint URL, 추가 필드(보증금/월세), gateway
rate-limit 키(`molit_rent`).

전세/월세 구분: API는 보증금(amount)과 월세금액(monthlyRent)을 모두 제공한다.
monthly_rent == 0 이면 전세, > 0 이면 월세로 라벨링.

사용 예:
    python -m real_estate.database.db_builder.build_apt_rent_db
        --lawd 11680 --ymd 202604
"""

import importlib.util
import subprocess
import sys

_REQUIRED_PIP = [("rapidfuzz", "rapidfuzz")]
_missing = [pip_name for pip_name, mod in _REQUIRED_PIP
            if importlib.util.find_spec(mod) is None]
if _missing:
    print(f"설치 필요: {_missing}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *_missing])

import argparse
import json
import logging
import os
import sqlite3
from datetime import date
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from infra.env import load_env
from real_estate.database.db_builder.build_apt_sale_db import (
    SEOUL_LAWD_CDS,
    fuzzy_match_complex,
    load_complex_match_index,
)
from real_estate.database.schema import ensure_schema
from real_estate.external.molit_rtms import MolitRtmsClient

ENDPOINT = (
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"
)
API_NAME = "molit_rent"
DB_PATH = _PROJECT_ROOT / "real_estate" / "database" / "DB" / "real_estate.db"
PAGE_SIZE = 1000

logger = logging.getLogger("builder.apt_rent")


def parse_item(raw: dict, *, lawd_cd: str) -> dict:
    """MOLIT 전월세 XML item dict → DB insert용 row."""
    deposit_str = raw.get("보증금액", "").replace(",", "").strip()
    deposit = int(deposit_str) if deposit_str.isdigit() else None

    monthly_str = raw.get("월세금액", "").replace(",", "").strip()
    monthly_rent = int(monthly_str) if monthly_str.isdigit() else 0

    contract_type = (
        "전세" if monthly_rent == 0
        else "월세"
    )

    year = raw.get("년", "").strip()
    month = raw.get("월", "").strip().zfill(2)
    day_str = raw.get("일", "").strip()
    deal_day = int(day_str) if day_str.isdigit() else None
    deal_year_month = f"{year}{month}" if year and month else ""

    area_str = raw.get("전용면적", "").strip()
    try:
        area_sqm = float(area_str) if area_str else None
    except ValueError:
        area_sqm = None

    floor_str = raw.get("층", "").strip()
    floor = int(floor_str) if floor_str.lstrip("-").isdigit() else None

    build_year_str = raw.get("건축년도", "").strip()
    build_year = int(build_year_str) if build_year_str.isdigit() else None

    sub_code = raw.get("법정동읍면동코드", "").strip()
    bcode = f"{lawd_cd}{sub_code}" if sub_code and len(sub_code) == 5 else None

    use_renewal = 1 if raw.get("갱신요구권사용", "").strip().upper() == "Y" else 0

    prev_deposit_str = raw.get("종전계약보증금", "").replace(",", "").strip()
    previous_deposit = int(prev_deposit_str) if prev_deposit_str.isdigit() else None
    prev_monthly_str = raw.get("종전계약월세", "").replace(",", "").strip()
    previous_monthly = int(prev_monthly_str) if prev_monthly_str.isdigit() else None

    return {
        "deal_year_month": deal_year_month,
        "deal_day": deal_day,
        "deposit": deposit,
        "monthly_rent": monthly_rent,
        "contract_type": contract_type,
        "contract_period": raw.get("계약기간", "").strip() or None,
        "use_renewal_right": use_renewal,
        "previous_deposit": previous_deposit,
        "previous_monthly_rent": previous_monthly,
        "lawd_cd": lawd_cd,
        "legal_dong": raw.get("법정동", "").strip() or None,
        "bcode": bcode,
        "jibun": raw.get("지번", "").strip() or None,
        "apt_name": raw.get("아파트", "").strip(),
        "area_sqm": area_sqm,
        "floor": floor,
        "build_year": build_year,
        "raw_payload": json.dumps(raw, ensure_ascii=False),
    }


def build(
    *,
    lawd_cds: list[str],
    deal_ymds: list[str],
    db_path: Path = DB_PATH,
    service_key: str | None = None,
) -> dict:
    service_key = service_key or os.environ.get("MOLIT_RENT_KEY", "")
    if not service_key:
        raise RuntimeError(
            "MOLIT_RENT_KEY 환경변수가 비어 있습니다. .env에 설정하세요."
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        ensure_schema(conn)
        match_index = load_complex_match_index(conn)
        client = MolitRtmsClient(service_key=service_key, api_name=API_NAME)

        total_fetched = 0
        total_inserted = 0
        total_matched = 0
        cur = conn.cursor()

        for lawd_cd in lawd_cds:
            candidates = match_index.get(lawd_cd, [])
            for ymd in deal_ymds:
                fetched_for_ymd = 0
                for raw in client.iter_items(
                    ENDPOINT,
                    lawd_cd=lawd_cd,
                    deal_ymd=ymd,
                    page_size=PAGE_SIZE,
                ):
                    fetched_for_ymd += 1
                    row = parse_item(raw, lawd_cd=lawd_cd)
                    master_id = fuzzy_match_complex(row["apt_name"], candidates)
                    row["master_complex_id"] = master_id
                    if master_id is not None:
                        total_matched += 1

                    result = cur.execute(
                        """
                        INSERT OR IGNORE INTO apt_rent_transaction (
                            deal_year_month, deal_day,
                            deposit, monthly_rent, contract_type, contract_period,
                            use_renewal_right, previous_deposit, previous_monthly_rent,
                            lawd_cd, legal_dong, bcode, jibun,
                            apt_name, area_sqm, floor, build_year,
                            master_complex_id, raw_payload
                        ) VALUES (
                            :deal_year_month, :deal_day,
                            :deposit, :monthly_rent, :contract_type, :contract_period,
                            :use_renewal_right, :previous_deposit, :previous_monthly_rent,
                            :lawd_cd, :legal_dong, :bcode, :jibun,
                            :apt_name, :area_sqm, :floor, :build_year,
                            :master_complex_id, :raw_payload
                        )
                        """,
                        row,
                    )
                    if result.rowcount:
                        total_inserted += 1
                conn.commit()
                total_fetched += fetched_for_ymd
                logger.info(
                    "LAWD_CD %s DEAL_YMD %s: fetched=%d",
                    lawd_cd, ymd, fetched_for_ymd,
                )

        match_rate = (total_matched / total_fetched * 100.0) if total_fetched else 0.0
        summary = {
            "lawd_cds": len(lawd_cds),
            "deal_ymds": len(deal_ymds),
            "total_fetched": total_fetched,
            "total_inserted": total_inserted,
            "total_matched": total_matched,
            "match_rate_percent": round(match_rate, 2),
        }
        logger.info("DONE %s", summary)
        return summary
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

    parser = argparse.ArgumentParser(description="MOLIT 아파트 전월세 실거래가 적재")
    parser.add_argument("--lawd", nargs="+", default=SEOUL_LAWD_CDS)
    parser.add_argument("--ymd", nargs="+", default=[_default_recent_ymd()])
    args = parser.parse_args()

    summary = build(lawd_cds=args.lawd, deal_ymds=args.ymd)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
