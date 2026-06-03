"""build_apt_sale_db.py — MOLIT 아파트 매매 실거래가 → real_estate.db 적재.

국토교통부 RTMS (실거래가 공개시스템) `getRTMSDataSvcAptTrade`로부터
시군구(LAWD_CD) × 거래연월(DEAL_YMD) 조합별 거래 내역을 받아
`apt_sale_transaction` 테이블에 적재한다.

각 거래 row는 단지명(아파트) free-text를 가진다. 같은 LAWD_CD 범위 내의
`apartment_complex` 마스터 단지명과 fuzzy 매칭(token_set_ratio ≥ 임계값)에
성공하면 `master_complex_id`를 채우고, 실패하면 NULL 보존 후 추후
validator/리뷰 단계에서 처리한다.

증분 적재 정책:
    UNIQUE (lawd_cd, deal_year_month, deal_day, apt_name, area_sqm, floor,
            deal_amount, jibun) 제약을 이용해 INSERT OR IGNORE.

사용 예:
    # 환경변수 MOLIT_REAL_TRANSACTION_KEY 설정 필요
    python -m real_estate.database.db_builder.build_apt_sale_db
        --lawd 11680 11650 11710
        --ymd 202604 202605

    인자 생략 시 기본값: 서울 25개 자치구 × 최근 1개월.
"""

# ===========================================================================
# Section 0: Imports / config
# ===========================================================================
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

from rapidfuzz import fuzz

# infra import를 위해 프로젝트 루트를 sys.path에 추가
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from infra.env import load_env
from real_estate.database.schema import ensure_schema
from real_estate.external.molit_rtms import MolitRtmsClient

# ===========================================================================
# Section 1: 상수
# ===========================================================================
ENDPOINT = (
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
)
API_NAME = "molit_real_transaction"
DB_PATH = _PROJECT_ROOT / "real_estate" / "database" / "DB" / "real_estate.db"
PAGE_SIZE = 1000

# 서울 25개 자치구 LAWD_CD
SEOUL_LAWD_CDS = [
    "11110", "11140", "11170", "11200", "11215", "11230", "11260", "11290",
    "11305", "11320", "11350", "11380", "11410", "11440", "11470", "11500",
    "11530", "11545", "11560", "11590", "11620", "11650", "11680", "11710",
    "11740",
]

# 단지명 fuzzy match 임계값 (token_set_ratio %).
# 너무 낮추면 동일구 안의 다른 단지 오매칭, 너무 높이면 표기 변형 미매칭.
NAME_MATCH_THRESHOLD = 85

logger = logging.getLogger("builder.apt_sale")


# ===========================================================================
# Section 2: 매칭 인덱스 (LAWD_CD → 단지 후보)
# ===========================================================================
def load_complex_match_index(conn: sqlite3.Connection) -> dict[str, list[tuple[int, str]]]:
    """LAWD_CD(시군구 5자리)별 (master_complex_id, canonical_name) 후보 인덱스.

    apartment_complex.legal_address 또는 road_address의 첫 두 토큰
    (예: "서울특별시 강남구")으로 LAWD_CD 5자리에 매핑한다. LAWD_CD ↔ 시군구명
    매핑이 정확하지 않은 경우엔 fallback으로 전체 master를 후보로 둘 수 있으나,
    여기선 행정구역명 부분문자열 매칭으로 후보를 좁힌다.
    """
    sigungu_names = _build_lawd_to_sigungu_name()
    cur = conn.execute(
        "SELECT master_complex_id, canonical_name, road_address, legal_address "
        "FROM apartment_complex"
    )
    by_lawd: dict[str, list[tuple[int, str]]] = {lawd: [] for lawd in sigungu_names}
    for master_id, canonical, road, legal in cur:
        addr = (road or "") + " " + (legal or "")
        for lawd_cd, sigungu in sigungu_names.items():
            if sigungu and sigungu in addr:
                by_lawd[lawd_cd].append((master_id, canonical or ""))
                break
    for lawd_cd, candidates in by_lawd.items():
        logger.info("LAWD_CD %s: %d candidates", lawd_cd, len(candidates))
    return by_lawd


def _build_lawd_to_sigungu_name() -> dict[str, str]:
    """Seoul 25 자치구만 매핑. 다른 지역이 추가될 때 확장."""
    return {
        "11110": "종로구", "11140": "중구", "11170": "용산구", "11200": "성동구",
        "11215": "광진구", "11230": "동대문구", "11260": "중랑구", "11290": "성북구",
        "11305": "강북구", "11320": "도봉구", "11350": "노원구", "11380": "은평구",
        "11410": "서대문구", "11440": "마포구", "11470": "양천구", "11500": "강서구",
        "11530": "구로구", "11545": "금천구", "11560": "영등포구", "11590": "동작구",
        "11620": "관악구", "11650": "서초구", "11680": "강남구", "11710": "송파구",
        "11740": "강동구",
    }


def fuzzy_match_complex(
    apt_name: str,
    candidates: list[tuple[int, str]],
    *,
    threshold: int = NAME_MATCH_THRESHOLD,
) -> int | None:
    """단지명 기반 fuzzy 매칭. 임계값 미달이면 None."""
    if not apt_name or not candidates:
        return None
    best_score = -1
    best_id = None
    for master_id, canonical in candidates:
        score = fuzz.token_set_ratio(apt_name, canonical)
        if score > best_score:
            best_score = score
            best_id = master_id
    if best_score >= threshold:
        return best_id
    return None


# ===========================================================================
# Section 3: row 파싱
# ===========================================================================
def parse_item(raw: dict, *, lawd_cd: str) -> dict:
    """MOLIT XML item dict → DB insert용 row."""
    deal_amount_str = raw.get("거래금액", "").replace(",", "").strip()
    deal_amount = int(deal_amount_str) if deal_amount_str.isdigit() else None

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

    is_canceled = 1 if raw.get("해제여부", "").strip().upper() == "O" else 0
    cancel_date = raw.get("해제사유발생일", "").strip() or None

    # 법정동 코드 합치기: 시군구(LAWD_CD) + 법정동읍면동코드 5자리 = bcode 10자리
    sub_code = raw.get("법정동읍면동코드", "").strip()
    bcode = f"{lawd_cd}{sub_code}" if sub_code and len(sub_code) == 5 else None

    return {
        "deal_year_month": deal_year_month,
        "deal_day": deal_day,
        "deal_amount": deal_amount,
        "lawd_cd": lawd_cd,
        "legal_dong": raw.get("법정동", "").strip() or None,
        "bcode": bcode,
        "jibun": raw.get("지번", "").strip() or None,
        "road_name": raw.get("도로명", "").strip() or None,
        "apt_name": raw.get("아파트", "").strip(),
        "area_sqm": area_sqm,
        "floor": floor,
        "build_year": build_year,
        "is_canceled": is_canceled,
        "cancel_date": cancel_date,
        "raw_payload": json.dumps(raw, ensure_ascii=False),
    }


# ===========================================================================
# Section 4: 빌더 본체
# ===========================================================================
def build(
    *,
    lawd_cds: list[str],
    deal_ymds: list[str],
    db_path: Path = DB_PATH,
    service_key: str | None = None,
) -> dict:
    service_key = service_key or os.environ.get("MOLIT_REAL_TRANSACTION_KEY", "")
    if not service_key:
        raise RuntimeError(
            "MOLIT_REAL_TRANSACTION_KEY 환경변수가 비어 있습니다. .env에 설정하세요."
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
                        INSERT OR IGNORE INTO apt_sale_transaction (
                            deal_year_month, deal_day, deal_amount,
                            lawd_cd, legal_dong, bcode, jibun, road_name,
                            apt_name, area_sqm, floor, build_year,
                            is_canceled, cancel_date,
                            master_complex_id, raw_payload
                        ) VALUES (
                            :deal_year_month, :deal_day, :deal_amount,
                            :lawd_cd, :legal_dong, :bcode, :jibun, :road_name,
                            :apt_name, :area_sqm, :floor, :build_year,
                            :is_canceled, :cancel_date,
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


# ===========================================================================
# Section 5: CLI
# ===========================================================================
def _default_recent_ymd() -> str:
    today = date.today()
    # 실거래가는 등록까지 시차가 있으므로 한 달 전 권장.
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

    parser = argparse.ArgumentParser(description="MOLIT 아파트 매매 실거래가 적재")
    parser.add_argument(
        "--lawd", nargs="+", default=SEOUL_LAWD_CDS,
        help="LAWD_CD 5자리 리스트. 기본: 서울 25개 자치구",
    )
    parser.add_argument(
        "--ymd", nargs="+", default=[_default_recent_ymd()],
        help="DEAL_YMD YYYYMM 리스트. 기본: 한 달 전",
    )
    args = parser.parse_args()

    summary = build(lawd_cds=args.lawd, deal_ymds=args.ymd)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
