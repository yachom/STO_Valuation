"""LOCALDATA(지방행정 인허가) 폐업 이력 수집 시험.

필요 env: LOCALDATA_API_KEY (data.go.kr 활용신청 또는 레거시 authKey)
사용:
    python -m scripts.try_localdata \
        --opn-svc-id 07_24_04_P --local-code 3160000 --closed-only
    python -m scripts.try_localdata --base-url <data.go.kr 이전 엔드포인트>

주의: localdata.go.kr 레거시 엔드포인트는 2026-04-16 포털 폐쇄로 다운 상태.
data.go.kr 이전 openDataApi 엔드포인트가 확정되면 --base-url 로 주입해 호출.
키/엔드포인트가 없으면 정규화 파서를 합성 envelope로 검증만 한다.
"""

import argparse
import json
import logging
import os

from infra.env import load_env
from real_estate.external.localdata import LocalDataClient, LocalDataError
from real_estate.external.localdata.client import _LEGACY_BASE


def _selftest_parser() -> None:
    """엔드포인트 없이 정규화 로직만 검증(합성 레거시 envelope)."""
    sample = {
        "result": {
            "header": {
                "process": {"code": "00", "message": "정상"},
                "paging": {"totalCount": 2, "pageIndex": 1, "pageSize": 500},
            },
            "body": {
                "rows": [
                    {
                        "row": [
                            {
                                "mgtNo": "X-1", "opnSvcId": "07_24_04_P",
                                "opnSvcNm": "일반음식점", "bplcNm": "마곡김밥",
                                "rdnWhlAddr": "서울특별시 강서구 마곡중앙로 1",
                                "siteWhlAddr": "서울특별시 강서구 마곡동 700",
                                "trdStateGbn": "03", "trdStateNm": "폐업",
                                "apvPermYmd": "20180301", "dcbYmd": "20231115",
                                "x": "190000.1", "y": "450000.2",
                                "lastModTs": "20231116000000",
                            },
                            {
                                "mgtNo": "X-2", "opnSvcNm": "일반음식점",
                                "bplcNm": "마곡카페",
                                "rdnWhlAddr": "서울특별시 강서구 마곡중앙로 1",
                                "trdStateGbn": "01", "trdStateNm": "영업/정상",
                                "apvPermYmd": "20240101", "dcbYmd": "",
                            },
                        ]
                    }
                ]
            },
        }
    }
    client = LocalDataClient.__new__(LocalDataClient)  # 파서만 쓰려고 우회 생성
    rows = []
    for block in sample["result"]["body"]["rows"]:
        for r in block["row"]:
            rows.append(client._normalize(r))
    print("=== 파서 자체검증 (합성 envelope) ===")
    for r in rows:
        print(f"  {r.bplc_nm} | {r.trd_state_nm} | 폐업판정={r.is_closed} | "
              f"dcbYmd={r.dcb_ymd} | {r.rdn_whl_addr}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    parser = argparse.ArgumentParser()
    parser.add_argument("--opn-svc-id", default="07_24_04_P")  # 일반음식점
    parser.add_argument("--local-code", default="3160000")     # 강서구 후보
    parser.add_argument("--base-url", default=_LEGACY_BASE)
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument("--closed-only", action="store_true")
    parser.add_argument("--addr-contains", default=None,
                        help="도로명주소 필터(예: 마곡)")
    args = parser.parse_args()

    key = os.environ.get("LOCALDATA_API_KEY")
    if not key:
        print("LOCALDATA_API_KEY env 비어 있음 — 실제 호출 생략, 파서만 검증.\n")
        _selftest_parser()
        return

    client = LocalDataClient(key, base_url=args.base_url)
    fetch = client.fetch_closed if args.closed_only else client.fetch_licenses
    try:
        rows = fetch(
            opn_svc_id=args.opn_svc_id,
            local_code=args.local_code,
            max_rows=args.max_rows,
        )
    except LocalDataError as exc:
        print(f"호출 실패: {exc}")
        print("(레거시 엔드포인트 다운일 수 있음. --base-url 로 data.go.kr 이전 "
              "엔드포인트를 지정하세요.)\n")
        _selftest_parser()
        return

    if args.addr_contains:
        rows = [r for r in rows if (r.rdn_whl_addr or "").find(args.addr_contains) >= 0]

    closed = [r for r in rows if r.is_closed]
    print(f"\n총 {len(rows)}건 (폐업 {len(closed)}건)")
    for r in closed[:20]:
        print(json.dumps({
            "사업장": r.bplc_nm, "상태": r.trd_state_nm,
            "폐업일자": r.dcb_ymd, "도로명": r.rdn_whl_addr,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
