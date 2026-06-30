"""build_nts_business_db.py — 국세청 사업자등록 통계 적재.

상가 평가의 forward-looking 지표: 행정구역·업종 단위 분기별 신규/폐업/생존
사업자 수. 폐업률 ↑ → 상권 건강도 ↓ → 본 평가 대상의 임차 안정성 우려.

공공데이터포털 envelope을 PublicDataPortalClient로 통과. endpoint URL은
변동 가능성이 커 env/CLI override.

응답 필드 다중 fallback (한국어 키 우선):
    행정구역코드 / 시군구코드 / 행정동코드, 분기, 업종코드, 업종명,
    신규등록(또는 신규개업), 폐업, 생존 / 가동 / 등록사업자수.
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
from real_estate.external.public_data_portal import PublicDataPortalClient
from real_estate.database.schema import ensure_schema

DEFAULT_ENDPOINT = os.environ.get(
    "NTS_BUSINESS_ENDPOINT",
    "https://apis.data.go.kr/1611000/NTSBusinessRegistryStats/getNTSBRStats",
)
API_NAME = "nts_business_registry"
DB_PATH = _PROJECT_ROOT / "real_estate" / "database" / "DB" / "real_estate.db"

logger = logging.getLogger("builder.nts_business")


def _i(v):
    try: return int(str(v).replace(",", ""))
    except (TypeError, ValueError): return None


def parse_item(raw: dict) -> dict | None:
    """NTS item → DB row. 키 변형 fallback."""
    # 행정구역 코드 — 시군구 5자리 또는 행정동 10자리
    region_cd = (
        raw.get("행정구역코드") or raw.get("행정동코드") or raw.get("시군구코드")
        or raw.get("region_cd") or raw.get("REGION_CD")
    )
    if not region_cd:
        return None
    region_cd = str(region_cd)
    region_level = "adstrd" if len(region_cd) == 10 else "sigungu"
    region_name = (
        raw.get("행정구역명") or raw.get("시군구명") or raw.get("region_name")
    )

    qc = (raw.get("분기") or raw.get("자료분기") or raw.get("quarter") or "").strip()
    if "년" in qc and "분기" in qc:
        year = qc.split("년")[0].strip()
        q = qc.split("년")[1].split("분기")[0].strip()
        qc = f"{year}{q}"

    industry_code = (
        raw.get("업종코드") or raw.get("산업분류코드") or raw.get("industry_code")
    )
    if not industry_code:
        return None
    industry_name = (
        raw.get("업종명") or raw.get("산업분류명") or raw.get("industry_name")
    )

    new_count = _i(raw.get("신규등록") or raw.get("신규개업") or raw.get("new_count"))
    closed_count = _i(raw.get("폐업") or raw.get("closed_count"))
    surviving_count = _i(
        raw.get("생존") or raw.get("가동") or raw.get("등록사업자수")
        or raw.get("surviving_count")
    )

    return {
        "region_cd": region_cd,
        "region_level": region_level,
        "region_name": region_name,
        "quarter_code": qc,
        "industry_code": str(industry_code),
        "industry_name": industry_name,
        "new_count": new_count,
        "closed_count": closed_count,
        "surviving_count": surviving_count,
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
    service_key = service_key or os.environ.get("NTS_BUSINESS_KEY", "")
    if not service_key:
        raise RuntimeError("NTS_BUSINESS_KEY env가 비어 있습니다.")

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
                params["분기"] = quarter
            try:
                items = client.get_items(endpoint, params)
            except Exception as exc:
                logger.warning("NTS get_items 실패 page=%d: %s", page, exc)
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
                    INSERT OR REPLACE INTO business_registry_stats_quarterly (
                        region_cd, region_level, region_name, quarter_code,
                        industry_code, industry_name,
                        new_count, closed_count, surviving_count,
                        raw_payload
                    ) VALUES (
                        :region_cd, :region_level, :region_name, :quarter_code,
                        :industry_code, :industry_name,
                        :new_count, :closed_count, :surviving_count,
                        :raw_payload
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
    parser.add_argument("--quarter", default=None)
    args = parser.parse_args()
    summary = build(endpoint=args.endpoint, quarter=args.quarter)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
