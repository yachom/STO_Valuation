"""건축HUB 건축물대장 — 부동산 전용 graphflow 도구.

infra/external/building_hub의 BuildingRegisterRepository(검증된 클라이언트)를
BaseTool로 감싸 트레이싱을 자동으로 얻는다. 부동산에서만 의미가 있으므로
infra가 아니라 real_estate 하위에 둔다 (자산 공통 도구는 infra/tool).

표제부에서 보고서의 물리 데이터 공백(연면적·층수·구조·용도·준공·에너지등급)을
메운다 — 첫 보고서가 "물리적 가치 평가에 한계"라 한탄한 바로 그 데이터.
"""

from __future__ import annotations

import logging

from real_estate.external.public_data_portal import PublicDataPortalClient
from infra.tool.base import BaseTool
from real_estate.external.building_hub import API_NAME, BuildingRegisterRepository
from real_estate.location import LocationInfo

logger = logging.getLogger("graphflow.tool.building_hub")


# 표제부 한글 키 → Building 노드 프로퍼티 (보고서 물리 평가에 쓰는 핵심만 발췌)
_TITLE_TO_PROPS = {
    "연면적": "total_area_sqm",
    "건축면적": "arch_area_sqm",
    "건폐율": "building_coverage_ratio",
    "용적률": "floor_area_ratio",
    "구조": "structure",
    "주용도": "main_use",
    "지상층수": "floors_above",
    "지하층수": "floors_below",
    "높이": "height_m",
    "사용승인일": "approved_at",
    "에너지효율등급": "energy_grade",
    "에너지절감율": "energy_reduce_rate",
    "세대수": "household_count",
    "내진설계적용여부": "seismic_design_applied",   # '0'/'1' — ESG 물리리스크(지진)
    "내진능력": "seismic_ability",
}


class BuildingRegisterTool(BaseTool):
    """LocationInfo → 건축물대장 표제부/층별/용도지역. Building 노드 보강용 props 포함."""

    name = "building_register"

    def __init__(self, service_key: str):
        super().__init__()
        client = PublicDataPortalClient(service_key=service_key, api_name=API_NAME)
        self._repo = BuildingRegisterRepository(client=client)

    def _run(self, *, location: LocationInfo):
        raw = self._repo.fetch(location)        # {표제부, 층별개요, 지역지구구역}
        title = raw.get("표제부") or {}
        props = {}
        for kr, prop in _TITLE_TO_PROPS.items():
            v = title.get(kr)
            if v is not None and v != "":
                props[prop] = v
        # 건물명 보강 (cert 매칭 실패 시 대장 건물명 사용)
        if title.get("건물명"):
            props["name"] = title["건물명"]
        # 용도지역 (일반상업지역 등) — 상권/입지 맥락
        zones = [z.get("명칭") for z in (raw.get("지역지구구역") or []) if z.get("명칭")]
        if zones:
            props["use_district"] = ", ".join(dict.fromkeys(zones))
        return {"building_props": props, "raw": raw}
