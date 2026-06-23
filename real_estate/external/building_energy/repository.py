"""건축HUB 건물에너지정보 — 건물 전기+가스 사용량 (data.go.kr 15135963).

월별 전기(getBeElctyUsgInfo)·가스(getBeGasUsgInfo) 사용량(useQty, kWh)을 연 단위로
합산하고, 1차에너지 환산계수(전기 2.75 / 가스 1.1)를 적용해 건축물대장 연면적으로 나눠
**실사용 기반 1차에너지 원단위(kWh/㎡·년)**를 낸다 → ESG-E(에너지)의 정량 grounding.

⚠️ 전기 환산계수가 가스의 2.5배라 전기가 등급을 좌우한다 — 가스단독은 허위로 좋게
나오므로(실측: 가스만 27.7→1+++ vs 가스+전기 630→7) 반드시 둘 다 합산한다.

건축물대장(building_hub)과 동일 키(DATA_GO_KR)·식별자(sigunguCd/bjdongCd/bun/ji),
추가로 useYm(YYYYMM) 필수. 등급은 '실사용 추정'으로 공식 인증(설계기준)과 구분.
"""

from __future__ import annotations

import logging

from infra.external.public_data_portal import PublicDataPortalClient
from real_estate.location import LocationInfo

API_NAME = "building_energy"
logger = logging.getLogger("external.building_energy")

# 1차에너지 환산계수
GAS_PRIMARY_FACTOR = 1.1
ELEC_PRIMARY_FACTOR = 2.75

# 비주거 연간 단위면적당 1차에너지소요량(kWh/㎡·년) 인증 등급표 — (등급, upper_exclusive)
NONRES_GRADE_TABLE = [
    ("1+++", 80), ("1++", 140), ("1+", 200), ("1", 260), ("2", 320),
    ("3", 380), ("4", 450), ("5", 520), ("6", 610), ("7", 700),
]


def _to_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def grade_for_primary(primary_kwh_sqm: float) -> str:
    """1차에너지 원단위(kWh/㎡·년) → 비주거 등급. 700 이상은 등급외."""
    for g, upper in NONRES_GRADE_TABLE:
        if primary_kwh_sqm < upper:
            return g
    return "등급외(700+)"


def primary_energy_assessment(gas_kwh: float, elec_kwh: float,
                              total_area_sqm: float) -> dict:
    """연간 가스+전기(kWh) + 연면적 → 실사용 1차에너지 원단위 + 추정 등급."""
    if not total_area_sqm or total_area_sqm <= 0:
        return {}
    g, e = gas_kwh or 0, elec_kwh or 0
    if g <= 0 and e <= 0:
        return {}
    primary = round((g * GAS_PRIMARY_FACTOR + e * ELEC_PRIMARY_FACTOR) / total_area_sqm, 1)
    return {
        "energy_primary_kwh_sqm": primary,
        "energy_grade_est": grade_for_primary(primary),   # 실사용 추정 등급(≠공식 설계기준 인증)
        "gas_intensity_kwh_sqm": round(g / total_area_sqm, 1) if g else None,
        "elec_intensity_kwh_sqm": round(e / total_area_sqm, 1) if e else None,
    }


class BuildingEnergyRepository:
    """건축HUB 건물에너지(전기+가스) 조회. building_hub와 동일 식별자·인증키."""

    ENDPOINT_GAS = "https://apis.data.go.kr/1613000/BldEngyHubService/getBeGasUsgInfo"
    ENDPOINT_ELEC = "https://apis.data.go.kr/1613000/BldEngyHubService/getBeElctyUsgInfo"

    def __init__(self, client: PublicDataPortalClient):
        self._client = client

    def _base(self, location: LocationInfo) -> dict:
        if location.bcode is None or location.bun is None:
            raise ValueError("BuildingEnergyRepository는 bcode와 bun이 필요합니다.")
        return {
            "sigunguCd": location.bcode[:5],
            "bjdongCd": location.bcode[5:],
            "bun": str(location.bun).zfill(4),
            "ji": str(location.ji).zfill(4) if location.ji is not None else "0000",
        }

    def _annual(self, endpoint: str, base: dict, year: int) -> tuple[float, int]:
        """endpoint(전기/가스) 해당 연도 12개월 합산 → (annual_kwh, n_months)."""
        monthly: dict[str, float] = {}
        for m in range(1, 13):
            ym = f"{year}{m:02d}"
            try:
                items = self._client.get_items(endpoint, params={**base, "useYm": ym})
            except Exception as exc:
                logger.debug("%s %s 조회 실패: %s", endpoint.rsplit("/", 1)[-1], ym, exc)
                continue
            for it in items:
                q = _to_float(it.get("useQty"))
                if q is not None:
                    monthly[ym] = monthly.get(ym, 0.0) + q
        return round(sum(monthly.values()), 1), len(monthly)

    def fetch_energy(self, location: LocationInfo, *, this_year: int,
                     years_back=(1, 2, 3), min_months: int = 6) -> dict:
        """전기+가스 모두 ≥min_months 데이터가 있는 최근 연도 채택(데이터 lag 대응).
        반환: {year, gas_kwh, elec_kwh, gas_months, elec_months}."""
        base = self._base(location)
        best = None
        for d in years_back:
            y = this_year - d
            gas, gm = self._annual(self.ENDPOINT_GAS, base, y)
            elec, em = self._annual(self.ENDPOINT_ELEC, base, y)
            row = {"year": y, "gas_kwh": gas, "elec_kwh": elec,
                   "gas_months": gm, "elec_months": em}
            if gm >= min_months and em >= min_months:
                return row
            if best is None or (gm + em) > (best["gas_months"] + best["elec_months"]):
                best = row
        return best or {"year": this_year - 1, "gas_kwh": 0.0, "elec_kwh": 0.0,
                        "gas_months": 0, "elec_months": 0}
