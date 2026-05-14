#!/usr/bin/env python3
"""Probe external real-estate APIs without printing secret keys."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def public_data_service_key() -> str | None:
    key = os.getenv("MOLIT_SERVICE_KEY") or os.getenv("DATA_GO_KR_SERVICE_KEY")
    return urllib.parse.unquote(key) if key else None


def request_text(url: str, params: dict[str, Any]) -> dict[str, Any]:
    clean_params = {key: value for key, value in params.items() if value not in (None, "")}
    request_url = f"{url}?{urllib.parse.urlencode(clean_params)}"
    request = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": "fnpricing-api-probe/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {"status": response.status, "body_preview": body[:700]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"status": exc.code, "error": exc.reason, "body_preview": body[:700]}
    except urllib.error.URLError as exc:
        return {"status": None, "error": str(exc.reason), "body_preview": ""}


def probe_vworld(pnu: str) -> dict[str, Any]:
    key = os.getenv("VWORLD_API_KEY")
    domain = os.getenv("VWORLD_DOMAIN")
    if not key:
        return {"skipped": "VWORLD_API_KEY is not set."}

    endpoints = ["ladfrlList", "getLandCharacteristics", "getLandUseAttr", "getIndvdLandPriceAttr"]
    return {
        endpoint: request_text(
            f"https://api.vworld.kr/ned/data/{endpoint}",
            {
                "key": key,
                "domain": domain,
                "pnu": pnu,
                "format": "json",
                "numOfRows": 1,
                "pageNo": 1,
            },
        )
        for endpoint in endpoints
    }


def probe_building(ids: dict[str, str]) -> dict[str, Any]:
    key = public_data_service_key()
    if not key:
        return {"skipped": "MOLIT_SERVICE_KEY or DATA_GO_KR_SERVICE_KEY is not set."}

    bases = {
        "hub": "https://apis.data.go.kr/1613000/BldRgstHubService",
        "legacy_v2": "https://apis.data.go.kr/1613000/BldRgstService_v2",
    }
    endpoints = ["getBrTitleInfo", "getBrBasisOulnInfo", "getBrFlrOulnInfo"]
    params = {
        "serviceKey": key,
        "sigunguCd": ids["sigungu_cd"],
        "bjdongCd": ids["bjdong_cd"],
        "platGbCd": "0" if ids["land_type_code"] == "1" else "1",
        "bun": ids["bun"],
        "ji": ids["ji"],
        "_type": "json",
        "numOfRows": 1,
        "pageNo": 1,
    }
    return {
        base_name: {
            endpoint: request_text(f"{base_url}/{endpoint}", params)
            for endpoint in endpoints
        }
        for base_name, base_url in bases.items()
    }


def split_pnu(pnu: str) -> dict[str, str]:
    if len(pnu) != 19 or not pnu.isdigit():
        raise ValueError("PNU must be 19 digits.")
    return {
        "pnu": pnu,
        "sigungu_cd": pnu[:5],
        "bjdong_cd": pnu[5:10],
        "land_type_code": pnu[10],
        "bun": pnu[11:15],
        "ji": pnu[15:19],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe VWorld and building-register APIs.")
    parser.add_argument("--pnu", default="1174010700105020004")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    load_dotenv(Path(args.env))
    ids = split_pnu(args.pnu)
    result = {
        "pnu": args.pnu,
        "vworld": probe_vworld(args.pnu),
        "building_register": probe_building(ids),
        "interpretation_hints": {
            "vworld_incorrect_key": "VWorld 문서상 INCORRECT_KEY는 등록되지 않은 키이거나 키 발급 시 등록한 도메인이 다른 경우에 발생할 수 있습니다.",
            "building_403": "공공데이터포털 인증키는 유효해도 건축HUB 건축물대장정보 서비스 활용신청/승인이 없으면 403이 발생할 수 있습니다.",
            "building_legacy_500": "BldRgstService_v2는 구 서비스입니다. 이 프로젝트 기본값은 최신 BldRgstHubService입니다.",
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
