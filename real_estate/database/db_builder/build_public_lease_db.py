"""build_public_lease_db.py — LH 공공임대주택 단지 → public_lease_complex.

LH OpenAPI (`lhLeaseNoticeInfo*` family)는 분양/임대 공고를 페이지 단위로 제공.
공고 단위가 아니라 '단지' 단위를 메모리에 합산해 INSERT OR REPLACE 한다.

같은 단지명+주소가 여러 공고에 등장할 수 있으므로 자연 키는 (lh_complex_id)
또는 합성 (region_name + complex_name + lease_type).

사용:
    python -m real_estate.database.db_builder.build_public_lease_db
    환경변수 LH_PUBLIC_HOUSING_KEY 필요.
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
from real_estate.database.schema import ensure_schema
from real_estate.external.lh import LhClient

ENDPOINT = "https://apis.data.go.kr/B552555/lhLeaseNoticeInfo3/listLHLeaseNoticeInfo"
DB_PATH = _PROJECT_ROOT / "real_estate" / "database" / "DB" / "real_estate.db"

logger = logging.getLogger("builder.public_lease")


def _normalize(item: dict) -> dict | None:
    """LH 원본 item → public_lease_complex row."""
    # LH 응답 필드명은 endpoint별로 다양. 흔한 명세 기준 best-effort.
    complex_name = item.get("BSNS_DETAIL_NM") or item.get("HSE_NM") or item.get("HSPCN_NM")
    if not complex_name:
        return None

    region = " ".join(filter(None, [item.get("CNP_NM"), item.get("CTPV_NM"), item.get("SIGUNGU_NM")]))
    legal_addr = item.get("HSE_LCT") or item.get("HSE_ADR") or item.get("LCT_ADR") or ""
    road_addr = item.get("ROAD_NM_HSE_LCT") or item.get("HSE_ROAD_ADR")
    bcode = item.get("BJDONG_CD") or item.get("LGDNG_CD")
    sigungu = (bcode or "")[:5] if bcode else item.get("SIGUNGU_CD")
    lease_type = item.get("LEASE_NM") or item.get("HSE_TY_NM") or item.get("BSNS_PRPS_NM")

    def _int_or_none(v):
        try: return int(v)
        except (TypeError, ValueError): return None

    def _float_or_none(v):
        try: return float(v)
        except (TypeError, ValueError): return None

    household = _int_or_none(item.get("TOT_HSHLD_CO") or item.get("ALL_GSPL_CO"))
    completion = item.get("USE_INSP_PRSC_YMD") or item.get("MVIN_PRARNDE")

    natural_key = "|".join(filter(None, [
        region, complex_name, lease_type or "",
        str(household or "")
    ])) or complex_name

    return {
        "lh_complex_id": natural_key[:120],
        "complex_name": complex_name,
        "lease_type": lease_type,
        "region_name": region or None,
        "bcode": bcode if bcode and len(str(bcode)) == 10 else None,
        "sigungu_cd": sigungu,
        "road_address": road_addr,
        "legal_address": legal_addr,
        "latitude": _float_or_none(item.get("LCT_LAT")),
        "longitude": _float_or_none(item.get("LCT_LOT")),
        "household_count": household,
        "completion_date": completion,
        "raw_payload": json.dumps(item, ensure_ascii=False),
    }


def build(*, db_path: Path = DB_PATH, service_key: str | None = None) -> dict:
    service_key = service_key or os.environ.get("LH_PUBLIC_HOUSING_KEY", "")
    if not service_key:
        raise RuntimeError("LH_PUBLIC_HOUSING_KEY env가 비어 있습니다.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    client = LhClient(service_key=service_key)

    fetched = 0
    inserted = 0
    page = 1
    conn = sqlite3.connect(str(db_path), timeout=60)
    try:
        ensure_schema(conn)
        cur = conn.cursor()
        while True:
            body = client.get(ENDPOINT, params={"PAGE": str(page), "PER_PAGE": "500"})
            items = LhClient.extract_items(body)
            if not items:
                break
            for it in items:
                fetched += 1
                row = _normalize(it)
                if row is None:
                    continue
                result = cur.execute(
                    """
                    INSERT OR REPLACE INTO public_lease_complex (
                        lh_complex_id, complex_name, lease_type,
                        region_name, bcode, sigungu_cd,
                        road_address, legal_address, latitude, longitude,
                        household_count, completion_date, raw_payload
                    ) VALUES (
                        :lh_complex_id, :complex_name, :lease_type,
                        :region_name, :bcode, :sigungu_cd,
                        :road_address, :legal_address, :latitude, :longitude,
                        :household_count, :completion_date, :raw_payload
                    )
                    """,
                    row,
                )
                if result.rowcount:
                    inserted += 1
            conn.commit()
            logger.info("page %d: items=%d (cumulative fetched=%d, inserted=%d)",
                        page, len(items), fetched, inserted)
            if len(items) < 500:
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
    args = parser.parse_args()
    print(json.dumps(build(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
