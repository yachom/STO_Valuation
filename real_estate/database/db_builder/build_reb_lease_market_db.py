"""build_reb_lease_market_db.py — 한국부동산원 상업용부동산 임대동향조사 적재.

분기 단위로 시군구·건물유형별 공실률·임대료·투자수익률을 적재한다. 이건 상가
부동산 평가의 핵심 시장 지표 — 본 평가 대상이 위치한 시군구의 공실률이 곧
임대 안정성 risk premium이 된다.

공공데이터포털 엔벨로프(`PublicDataPortalClient`) 그대로 사용. endpoint URL은
환경변수 `REB_LEASE_ENDPOINT` 또는 CLI `--endpoint`로 override 가능 (한국
부동산원 OpenAPI는 endpoint 명이 자주 갱신되므로).

응답 필드는 API 버전 따라 다양해서 가장 흔한 한국어 키를 best-effort 매핑:
    시군구코드, 시군구명, 자료년월/자료기간/분기, 부동산유형(또는 상가구분),
    공실률, 임대료, 투자수익률, 자본수익률, 소득수익률.

사용:
    python -m real_estate.database.db_builder.build_reb_lease_market_db
        --endpoint https://apis.data.go.kr/...
        --quarter 20254
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
from infra.external.public_data_portal import PublicDataPortalClient
from real_estate.database.schema import ensure_schema

DEFAULT_ENDPOINT = os.environ.get(
    "REB_LEASE_ENDPOINT",
    # placeholder — 운영 시 정확한 endpoint로 override
    "https://apis.data.go.kr/1611000/REBLeaseTrendSvc/getRELeaseTrend",
)
API_NAME = "reb_lease_market"
DB_PATH = _PROJECT_ROOT / "real_estate" / "database" / "DB" / "real_estate.db"

logger = logging.getLogger("builder.reb_lease")


def _f(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def parse_item(raw: dict) -> dict | None:
    """REB item dict → DB row. 키 변형은 다중 fallback."""
    sigungu_cd = (
        raw.get("시군구코드") or raw.get("sigungu_cd") or raw.get("SIGUNGU_CD")
    )
    if not sigungu_cd:
        return None
    sigungu_name = (
        raw.get("시군구명") or raw.get("시군구") or raw.get("sigungu_name")
    )

    # 분기 코드 정규화: '20254' or '2025/4' or '2025년 4분기' → '20254'
    qc = (
        raw.get("자료기간") or raw.get("분기") or raw.get("자료년월") or ""
    ).strip()
    if "년" in qc and "분기" in qc:
        year = qc.split("년")[0].strip()
        q = qc.split("년")[1].split("분기")[0].strip()
        qc = f"{year}{q}"
    elif "/" in qc:
        qc = qc.replace("/", "")
    elif len(qc) == 6 and qc.isdigit():
        # YYYYMM → YYYYQ
        year = qc[:4]
        m = int(qc[4:])
        q = (m - 1) // 3 + 1
        qc = f"{year}{q}"

    building_type = (
        raw.get("부동산유형") or raw.get("상가구분")
        or raw.get("부동산유형구분") or raw.get("building_type")
    )
    if not building_type:
        return None

    return {
        "sigungu_cd": str(sigungu_cd),
        "sigungu_name": sigungu_name,
        "quarter_code": qc,
        "building_type": building_type,
        "vacancy_rate": _f(raw.get("공실률") or raw.get("vacancy_rate")),
        "rent_per_sqm": _f(raw.get("임대료") or raw.get("rent_per_sqm")),
        "yield_rate": _f(raw.get("투자수익률") or raw.get("yield_rate")),
        "capital_gain_rate": _f(raw.get("자본수익률") or raw.get("capital_gain_rate")),
        "income_return": _f(raw.get("소득수익률") or raw.get("income_return")),
        "raw_payload": json.dumps(raw, ensure_ascii=False),
    }


def build(
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    quarter: str | None = None,
    page_size: int = 1000,
    db_path: Path = DB_PATH,
    service_key: str | None = None,
) -> dict:
    service_key = service_key or os.environ.get("REB_LEASE_MARKET_KEY", "")
    if not service_key:
        raise RuntimeError("REB_LEASE_MARKET_KEY env가 비어 있습니다.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    client = PublicDataPortalClient(service_key=service_key, api_name=API_NAME)

    fetched = 0
    inserted = 0
    page = 1
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        while True:
            params = {"numOfRows": str(page_size), "pageNo": str(page)}
            if quarter:
                params["자료기간"] = quarter
            try:
                items = client.get_items(endpoint, params)
            except Exception as exc:
                logger.warning("REB get_items 실패 page=%d: %s", page, exc)
                break
            if not items:
                break
            for it in items:
                fetched += 1
                row = parse_item(it)
                if row is None:
                    continue
                result = cur.execute(
                    """
                    INSERT OR REPLACE INTO commercial_lease_market_quarterly (
                        sigungu_cd, sigungu_name, quarter_code, building_type,
                        vacancy_rate, rent_per_sqm, yield_rate,
                        capital_gain_rate, income_return, raw_payload
                    ) VALUES (
                        :sigungu_cd, :sigungu_name, :quarter_code, :building_type,
                        :vacancy_rate, :rent_per_sqm, :yield_rate,
                        :capital_gain_rate, :income_return, :raw_payload
                    )
                    """,
                    row,
                )
                if result.rowcount:
                    inserted += 1
            conn.commit()
            logger.info("page %d: items=%d (cum fetched=%d inserted=%d)",
                        page, len(items), fetched, inserted)
            if len(items) < page_size:
                break
            page += 1
    finally:
        conn.close()
    return {"fetched": fetched, "inserted": inserted, "pages": page}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--quarter", default=None,
                        help="자료기간 (예: 20254). 미지정 시 API 기본값")
    args = parser.parse_args()
    summary = build(endpoint=args.endpoint, quarter=args.quarter)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
