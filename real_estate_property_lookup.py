#!/usr/bin/env python3
"""Commercial property lookup helper for Korean commercial real estate.

The script turns an address, PNU, or lot-number parts into the identifiers most
Korean real-estate APIs need, then optionally calls public APIs for parcel,
building, land-use and commercial transaction reference data.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any


class LookupErrorWithContext(RuntimeError):
    """Raised when a remote API request fails with useful context."""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def request_json(url: str, params: dict[str, Any]) -> Any:
    clean_params = {key: value for key, value in params.items() if value not in (None, "")}
    encoded = urllib.parse.urlencode(clean_params)
    request_url = f"{url}?{encoded}" if encoded else url
    request = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": "fnpricing-property-lookup/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        hint = ""
        if "BldRgstHubService" in url and exc.code in {401, 403}:
            hint = " Check that 국토교통부_건축HUB_건축물대장정보 서비스 활용신청/승인이 이 인증키에 반영됐는지 확인하세요."
        raise LookupErrorWithContext(f"HTTP {exc.code} from {url}: {detail[:500]}{hint}") from exc
    except urllib.error.URLError as exc:
        raise LookupErrorWithContext(f"Network error from {url}: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        if not body:
            return {"empty": True, "message": "API returned an empty response body."}
        return {"raw": body}


def public_data_service_key() -> str | None:
    key = os.getenv("MOLIT_SERVICE_KEY") or os.getenv("DATA_GO_KR_SERVICE_KEY")
    return urllib.parse.unquote(key) if key else None


def safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # Keep one flaky public API from killing the whole report payload.
        return {
            "error": type(exc).__name__,
            "message": str(exc),
        }


def add_months(year_month: str, delta: int) -> str:
    year = int(year_month[:4])
    month = int(year_month[4:6])
    month_index = year * 12 + month - 1 + delta
    return f"{month_index // 12:04d}{month_index % 12 + 1:02d}"


def default_trade_period(months: int) -> tuple[str, str]:
    # Real-estate transaction APIs can be sparse or incomplete for the current month.
    end = add_months(date.today().strftime("%Y%m"), -1)
    start = add_months(end, -(months - 1))
    return start, end


def normalize_lot(main: str | int | None, sub: str | int | None = None) -> tuple[str, str]:
    main_text = str(main or "0").strip()
    sub_text = str(sub or "0").strip()
    return main_text.zfill(4), sub_text.zfill(4)


def pnu_from_parts(bjdong_code: str, land_type_code: str | int, main: str | int, sub: str | int = 0) -> str:
    main4, sub4 = normalize_lot(main, sub)
    return f"{str(bjdong_code).strip()[:10]}{int(land_type_code)}{main4}{sub4}"


def pnu_land_type_from_juso_mt_yn(mt_yn: str | int | None) -> str:
    return "2" if str(mt_yn or "0") == "1" else "1"


def building_plat_gb_cd_from_pnu_land_type(land_type_code: str) -> str:
    if land_type_code == "1":
        return "0"
    if land_type_code == "2":
        return "1"
    return land_type_code


def split_pnu(pnu: str) -> dict[str, str]:
    pnu = str(pnu).strip()
    if len(pnu) != 19 or not pnu.isdigit():
        raise ValueError(f"PNU must be 19 digits, got: {pnu}")
    return {
        "pnu": pnu,
        "bjdong_code": pnu[:10],
        "lawd_cd": pnu[:5],
        "sigungu_cd": pnu[:5],
        "bjdong_cd": pnu[5:10],
        "land_type_code": pnu[10],
        "bun": pnu[11:15],
        "ji": pnu[15:19],
    }


def pnu_from_juso(address: str) -> dict[str, Any]:
    key = os.getenv("JUSO_CONFIRM_KEY")
    if not key:
        raise LookupErrorWithContext("JUSO_CONFIRM_KEY is required for free-text address lookup.")

    data = request_json(
        "https://business.juso.go.kr/addrlink/addrLinkApi.do",
        {
            "confmKey": key,
            "currentPage": 1,
            "countPerPage": 5,
            "keyword": address,
            "resultType": "json",
        },
    )
    rows = data.get("results", {}).get("juso", [])
    if not rows:
        raise LookupErrorWithContext(f"No Juso result for address: {address}")

    selected = rows[0]
    pnu = pnu_from_parts(
        selected["admCd"],
        pnu_land_type_from_juso_mt_yn(selected.get("mtYn", "0")),
        selected.get("lnbrMnnm", "0"),
        selected.get("lnbrSlno", "0"),
    )
    return {
        "source": "juso_api",
        "address": {
            "input": address,
            "road": selected.get("roadAddr"),
            "jibun": selected.get("jibunAddr"),
            "building_management_no": selected.get("bdMgtSn"),
        },
        "identifiers": split_pnu(pnu),
        "juso_candidates": rows,
    }


def pnu_from_lot_args(args: argparse.Namespace) -> dict[str, Any]:
    pnu = pnu_from_parts(args.bjdong_code, args.land_type, args.main_lot, args.sub_lot)
    return {
        "source": "lot_parts",
        "input": {
            "bjdong_code": args.bjdong_code,
            "land_type": args.land_type,
            "main_lot": args.main_lot,
            "sub_lot": args.sub_lot,
        },
        "identifiers": split_pnu(pnu),
    }


def call_vworld(endpoint: str, pnu: str) -> Any:
    key = os.getenv("VWORLD_API_KEY")
    if not key:
        return {"skipped": "VWORLD_API_KEY is not set."}
    domain = os.getenv("VWORLD_DOMAIN")
    return request_json(
        f"https://api.vworld.kr/ned/data/{endpoint}",
        {
            "key": key,
            "domain": domain,
            "pnu": pnu,
            "format": "json",
            "numOfRows": 10,
            "pageNo": 1,
        },
    )


def call_building_register(endpoint: str, ids: dict[str, str]) -> Any:
    key = public_data_service_key()
    if not key:
        return {"skipped": "MOLIT_SERVICE_KEY or DATA_GO_KR_SERVICE_KEY is not set."}
    base_url = os.getenv("BUILDING_REGISTER_BASE_URL", "https://apis.data.go.kr/1613000/BldRgstHubService")
    return request_json(
        f"{base_url}/{endpoint}",
        {
            "serviceKey": key,
            "sigunguCd": ids["sigungu_cd"],
            "bjdongCd": ids["bjdong_cd"],
            "platGbCd": building_plat_gb_cd_from_pnu_land_type(ids["land_type_code"]),
            "bun": ids["bun"],
            "ji": ids["ji"],
            "_type": "json",
            "numOfRows": 20,
            "pageNo": 1,
        },
    )


def dataframe_to_rows(df: Any, max_rows: int) -> list[dict[str, Any]]:
    return json.loads(df.head(max_rows).to_json(orient="records", force_ascii=False, date_format="iso"))


def normalize_jibun(main: str, sub: str) -> str:
    main_int = str(int(main)) if str(main).isdigit() else str(main).lstrip("0")
    sub_int = str(int(sub)) if str(sub).isdigit() else str(sub).lstrip("0")
    return main_int if sub_int in {"", "0"} else f"{main_int}-{sub_int}"


def lot_mask_match(candidate: Any, target_lot: str) -> bool:
    if candidate is None:
        return False
    candidate_text = str(candidate).strip()
    if not candidate_text:
        return False
    if "*" not in candidate_text:
        return candidate_text == target_lot

    target_main = target_lot.split("-", 1)[0]
    candidate_main = candidate_text.split("-", 1)[0]
    if candidate_main.replace("*", "") == "":
        return True
    return target_main.startswith(candidate_main.replace("*", ""))


def filter_transaction_dataframe(
    df: Any,
    bjdong_name: str | None,
    target_lot: str | None,
    filter_mode: str,
) -> tuple[Any, dict[str, Any]]:
    if filter_mode == "none":
        return df, {"mode": "none", "applied": False}

    filtered = df
    filters: dict[str, Any] = {
        "mode": filter_mode,
        "applied": True,
        "input_row_count": int(len(df)),
    }

    if bjdong_name and "법정동" in filtered.columns:
        filtered = filtered[filtered["법정동"].astype(str).str.strip() == bjdong_name]
        filters["bjdong_name"] = bjdong_name
        filters["after_bjdong_count"] = int(len(filtered))

    if filter_mode == "lot" and target_lot and "지번" in filtered.columns:
        filtered = filtered[filtered["지번"].apply(lambda value: lot_mask_match(value, target_lot))]
        filters["target_lot"] = target_lot
        filters["lot_match_note"] = "마스킹 지번은 본번 prefix 기준으로만 후보 매칭합니다."
        filters["after_lot_count"] = int(len(filtered))

    filters["output_row_count"] = int(len(filtered))
    return filtered, filters


def dataframe_to_payload(df: Any, max_rows: int) -> dict[str, Any]:
    rows = dataframe_to_rows(df, max_rows)
    return {
        "row_count": int(len(df)),
        "returned_rows": len(rows),
        "rows": rows,
    }


def call_transaction_price(
    ids: dict[str, str],
    property_type: str,
    trade_type: str,
    year_month: str,
    start_year_month: str | None,
    end_year_month: str | None,
    max_rows: int,
    bjdong_name: str | None,
    target_lot: str | None,
    filter_mode: str,
) -> Any:
    key = public_data_service_key()
    if not key:
        return {"skipped": "MOLIT_SERVICE_KEY or DATA_GO_KR_SERVICE_KEY is not set."}
    try:
        from PublicDataReader import TransactionPrice
    except ImportError:
        return {
            "skipped": "PublicDataReader is not installed.",
            "install": "pip install PublicDataReader",
        }

    api = TransactionPrice(key)
    params = {
        "property_type": property_type,
        "trade_type": trade_type,
        "sigungu_code": ids["lawd_cd"],
    }
    if start_year_month or end_year_month:
        if not (start_year_month and end_year_month):
            raise ValueError("--start-deal-ym and --end-deal-ym must be used together.")
        params["start_year_month"] = start_year_month
        params["end_year_month"] = end_year_month
    else:
        params["year_month"] = year_month

    captured_stdout = io.StringIO()
    with contextlib.redirect_stdout(captured_stdout):
        df = api.get_data(**params)
    filtered_df, filters = filter_transaction_dataframe(df, bjdong_name, target_lot, filter_mode)
    payload = dataframe_to_payload(filtered_df, max_rows)
    payload["unfiltered_row_count"] = int(len(df))
    payload["filters"] = filters
    payload["query"] = params
    payload["library"] = "PublicDataReader.TransactionPrice"
    warnings = [line for line in captured_stdout.getvalue().splitlines() if line.strip()]
    if warnings:
        payload["warnings"] = warnings
    return payload


def official_document_checklist() -> list[dict[str, str]]:
    return [
        {
            "purpose": "토지 기본정보",
            "document_or_service": "토지대장 / 토지임야대장",
            "check": "지번, 지목, 면적, 토지이동 이력",
        },
        {
            "purpose": "건물 기본정보",
            "document_or_service": "건축물대장",
            "check": "대지위치, 연면적, 주용도, 층수, 사용승인일, 위반건축물 여부",
        },
        {
            "purpose": "집합건물 호실 확인",
            "document_or_service": "집합건축물 표제부 + 전유부",
            "check": "해당 호실 면적, 용도, 대지권 관련 정보",
        },
        {
            "purpose": "용도지역/규제",
            "document_or_service": "토지이용계획확인원 / 토지이음",
            "check": "용도지역, 지구, 행위제한, 토지거래허가구역 여부",
        },
        {
            "purpose": "권리관계",
            "document_or_service": "등기사항증명서",
            "check": "소유권, 근저당, 압류, 가압류, 전세권, 임차권등기, 대지권",
        },
    ]


def ownership_data_notice() -> dict[str, Any]:
    return {
        "available_from_open_apis": False,
        "reason": "현재 소유자, 소유권 이전 이력, 근저당/압류/가압류 등 권리관계는 공개 공공 Open API 조회 대상이 아니라 등기사항증명서 영역입니다.",
        "recommended_workflow": [
            "이 도구로 주소 기반 공개 공공데이터를 수집합니다.",
            "인터넷등기소에서 등기사항증명서를 사람이 직접 열람/발급합니다.",
            "발급받은 등기사항증명서 PDF를 별도 분석 단계에 입력해 리포트에 합칩니다.",
        ],
        "do_not_automate": [
            "대량 스크래핑",
            "캡차/인증/결제 우회",
            "약관상 허용되지 않는 자동 열람/발급",
        ],
    }


def subject_bjdong_name(subject: dict[str, Any]) -> str | None:
    candidates = subject.get("juso_candidates") or []
    if candidates:
        return candidates[0].get("emdNm") or None
    address = subject.get("address") or {}
    jibun = address.get("jibun")
    if isinstance(jibun, str):
        parts = jibun.split()
        for part in parts:
            if part.endswith(("동", "읍", "면", "리")):
                return part
    return None


def subject_target_lot(subject: dict[str, Any]) -> str | None:
    ids = subject.get("identifiers") or {}
    bun = ids.get("bun")
    ji = ids.get("ji")
    if bun and ji:
        return normalize_jibun(str(bun), str(ji))
    candidates = subject.get("juso_candidates") or []
    if candidates:
        selected = candidates[0]
        main = selected.get("lnbrMnnm")
        sub = selected.get("lnbrSlno") or "0"
        if main:
            return normalize_jibun(str(main), str(sub))
    return None


def build_result(subject: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    ids = subject["identifiers"]
    result: dict[str, Any] = {
        "generated_at": date.today().isoformat(),
        "subject": subject,
        "workflow": {
            "manual_reference": [
                "서울부동산정보광장 / 일사편리 / 지자체 부동산 종합정보",
                "건축물대장",
                "토지이용계획확인원 / 토지이음",
                "등기사항증명서",
            ],
            "automation": "주소 -> 법정동코드/지번/PNU -> 토지 API + 건축물대장 API + 토지이용계획 API + 실거래가 API",
        },
        "official_document_checklist": official_document_checklist(),
        "ownership_and_registry_notice": ownership_data_notice(),
    }

    if args.api in {"vworld", "all"}:
        result["vworld"] = {
            "land_forest_ledger": safe_call(call_vworld, "ladfrlList", ids["pnu"]),
            "land_characteristics": safe_call(call_vworld, "getLandCharacteristics", ids["pnu"]),
            "land_use": safe_call(call_vworld, "getLandUseAttr", ids["pnu"]),
            "official_land_price": safe_call(call_vworld, "getIndvdLandPriceAttr", ids["pnu"]),
        }

    if args.api in {"building", "all"}:
        result["building_register"] = {
            "basis": safe_call(call_building_register, "getBrBasisOulnInfo", ids),
            "title": safe_call(call_building_register, "getBrTitleInfo", ids),
            "recap_title": safe_call(call_building_register, "getBrRecapTitleInfo", ids),
            "exclusive_unit": safe_call(call_building_register, "getBrExposPubuseAreaInfo", ids),
            "exclusive_section": safe_call(call_building_register, "getBrExposInfo", ids),
            "floor": safe_call(call_building_register, "getBrFlrOulnInfo", ids),
            "zone": safe_call(call_building_register, "getBrJijiguInfo", ids),
        }

    if args.api in {"trade", "all"}:
        result["transaction_price"] = safe_call(
            call_transaction_price,
            ids,
            args.trade_property_type,
            args.trade_type,
            args.deal_ym,
            args.start_deal_ym,
            args.end_deal_ym,
            args.trade_max_rows,
            args.trade_filter_bjdong or subject_bjdong_name(subject),
            args.trade_filter_lot or subject_target_lot(subject),
            args.trade_filter_mode,
        )

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="주소 하나로 공개 공공데이터 기반 부동산 리포트 원천 JSON을 수집합니다."
    )
    parser.add_argument("address_text", nargs="?", help="조회할 주소. 예: '서울특별시 강동구 상암로3길 8'")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--address", help="행안부 주소 API로 검색할 주소")
    source.add_argument("--pnu", help="직접 입력한 19자리 PNU")
    source.add_argument("--bjdong-code", help="10자리 법정동코드. --main-lot과 함께 PNU를 생성합니다.")
    parser.add_argument("--land-type", choices=["1", "2"], default="1", help="PNU 토지구분. 1=대지, 2=산")
    parser.add_argument("--main-lot", help="지번 본번")
    parser.add_argument("--sub-lot", default="0", help="지번 부번. 없으면 0")
    parser.add_argument(
        "--api",
        choices=["none", "vworld", "building", "trade", "all"],
        help="외부 API 조회 범위. 기본값은 주소 입력 시 all, 고급 입력 시 none입니다.",
    )
    parser.add_argument(
        "--deal-ym",
        default=None,
        help="상업업무용 실거래가 조회 계약년월(YYYYMM)",
    )
    parser.add_argument("--start-deal-ym", help="실거래가 기간 조회 시작 년월(YYYYMM)")
    parser.add_argument("--end-deal-ym", help="실거래가 기간 조회 종료 년월(YYYYMM)")
    parser.add_argument("--trade-months", type=int, default=12, help="기간 미지정 시 실거래가 최근 조회 개월 수")
    parser.add_argument(
        "--trade-filter-mode",
        choices=["none", "dong", "lot"],
        default="dong",
        help="실거래가 후처리 필터. dong=법정동까지, lot=법정동+지번 후보까지",
    )
    parser.add_argument("--trade-filter-bjdong", help="실거래가 후처리 법정동명 override")
    parser.add_argument("--trade-filter-lot", help="실거래가 후처리 지번 override. 예: 21-15")
    parser.add_argument(
        "--trade-property-type",
        choices=["아파트", "오피스텔", "단독다가구", "연립다세대", "토지", "분양입주권", "상업업무용", "공장창고등"],
        default="상업업무용",
        help="PublicDataReader TransactionPrice 부동산 유형",
    )
    parser.add_argument(
        "--trade-type",
        choices=["매매", "전월세"],
        default="매매",
        help="PublicDataReader TransactionPrice 거래 유형",
    )
    parser.add_argument("--trade-max-rows", type=int, default=20, help="실거래가 출력 최대 행 수")
    parser.add_argument("--env", default=".env", help="읽을 env 파일 경로")
    parser.add_argument("--pretty", action="store_true", help="JSON을 보기 좋게 출력")
    return parser.parse_args()


def resolve_defaults(args: argparse.Namespace) -> None:
    if args.address_text and (args.address or args.pnu or args.bjdong_code):
        raise ValueError("Use either positional address or one explicit source option, not both.")

    has_address = bool(args.address_text or args.address)
    has_advanced_source = bool(args.pnu or args.bjdong_code)
    if not has_address and not has_advanced_source:
        raise ValueError("Address is required. Example: real_estate_property_lookup.py '서울특별시 강동구 상암로3길 8'")

    if args.api is None:
        args.api = "all" if has_address else "none"

    if args.api in {"trade", "all"} and not args.deal_ym and not (args.start_deal_ym or args.end_deal_ym):
        args.start_deal_ym, args.end_deal_ym = default_trade_period(args.trade_months)


def main() -> int:
    args = parse_args()
    load_dotenv(Path(args.env))
    resolve_defaults(args)

    address = args.address_text or args.address
    if address:
        subject = pnu_from_juso(address)
    elif args.pnu:
        subject = {"source": "manual_pnu", "identifiers": split_pnu(args.pnu)}
    else:
        if not args.main_lot:
            print("error: --main-lot is required with --bjdong-code", file=sys.stderr)
            return 1
        subject = pnu_from_lot_args(args)

    output = build_result(subject, args)
    print(json.dumps(output, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LookupErrorWithContext, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
