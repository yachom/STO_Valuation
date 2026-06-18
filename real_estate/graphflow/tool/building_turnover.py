"""건물 임차 회전율 — 부동산 전용 graphflow 도구.

real_estate/repository의 BuildingTurnoverRepository(LocalData 인허가 기반)를
BaseTool로 감싸 트레이싱을 자동으로 얻는다. tenant(임차) 축에 '이 건물에서 임차
사업자가 얼마나 자주 바뀌는가'라는 정량 회전율을 공급한다 — REB(지역 시장환경)와
달리 **이 건물 자체**의 폐업이력 기반 데이터.

입력은 도로명주소가 1차다. LocationInfo를 주면 road_address를 자동 추출한다.
미신청 업종은 LocalData가 403을 주므로 repository가 graceful skip한다.
"""

from __future__ import annotations

import logging

from infra.external.localdata.client import LocalDataClient
from infra.tool.base import BaseTool
from real_estate.location import LocationInfo
from real_estate.repository.building_turnover import BuildingTurnoverRepository

logger = logging.getLogger("graphflow.tool.building_turnover")


class BuildingTurnoverTool(BaseTool):
    """도로명주소(또는 LocationInfo) → 건물 임차 회전율 지표. tenant 축 정량 근거."""

    name = "building_turnover"

    def __init__(self, service_key: str, catalog: dict | None = None):
        super().__init__()
        self._repo = BuildingTurnoverRepository(
            LocalDataClient(service_key), catalog=catalog)

    def _run(self, *, location: LocationInfo | None = None,
             road_address: str | None = None,
             services: list[str] | None = None, recent_years: int = 2):
        """Args:
            location:     LocationInfo (road_address 자동 추출) — 1차 경로.
            road_address: 직접 지정 (location 없을 때).
            services:     수집할 업종 라벨 (None이면 catalog 전체).
            recent_years: '최근 N년' 회전 집계 창.

        Returns:
            BuildingTurnoverRepository.turnover() 반환 dict
            (입점이력/누적폐업률/평균존속연수/업태분포/수집업종 등).
        """
        if location is not None:
            road_address = road_address or location.road_address
        if not road_address:
            return {"입점이력": 0, "error": "location 또는 road_address가 필요합니다."}
        return self._repo.turnover(
            road_address, services=services, recent_years=recent_years)
