"""건물 입점업체 로스터 — 부동산 전용 graphflow 도구.

real_estate/repository의 BuildingTenantsRepository(Kakao Local 기반)를 BaseTool로
감싸 트레이싱을 자동으로 얻는다. tenant(임차) 축에 '이 건물에 실제로 누가
입점해 있는가'라는 정량 로스터를 공급한다 — 중심 테넌트 식별의 1차 입력.

리뷰/평점은 이 도구에 없다(공식 API 미제공). place_id만 확보하고, 후속
KakaoPlaceScraper가 리뷰를 보강한다.
"""

from __future__ import annotations

import collections
import logging

from infra.external.kakao_local import KakaoLocalClient
from infra.tool.base import BaseTool
from real_estate.location import LocationInfo
from real_estate.repository.building_tenants import BuildingTenantsRepository

logger = logging.getLogger("graphflow.tool.building_tenants")


class BuildingTenantsTool(BaseTool):
    """LocationInfo(또는 도로명+좌표) → 건물 입점업체 로스터 + 카테고리 분포."""

    name = "building_tenants"

    def __init__(self, kakao_rest_key: str):
        super().__init__()
        self._repo = BuildingTenantsRepository(KakaoLocalClient(kakao_rest_key))

    def _run(self, *, location: LocationInfo | None = None,
             road_address: str | None = None, x: float | None = None,
             y: float | None = None, building_name: str | None = None,
             radius: int = 50):
        """Args:
            location:     LocationInfo (road_address·좌표 자동 추출) — 1차 경로.
            road_address: 직접 지정 (location 없을 때).
            x, y:         경도·위도.
            building_name: 건물명 (키워드 검색 보강).

        Returns:
            {"건물": str, "입점업체수": int,
             "카테고리분포": {대분류: 개수}, "입점업체": [tenant dict, ...]}
        """
        if location is not None:
            road_address = road_address or location.road_address
            x = x if x is not None else location.longitude
            y = y if y is not None else location.latitude
        if not road_address:
            return {"입점업체수": 0, "입점업체": [],
                    "error": "location 또는 road_address가 필요합니다."}

        tenants = self._repo.fetch(
            road_address=road_address, x=x, y=y,
            building_name=building_name, radius=radius)
        dist = collections.Counter(
            (t["카테고리"].split(" > ")[0] if t["카테고리"] else "기타")
            for t in tenants)
        return {
            "건물": building_name or road_address,
            "입점업체수": len(tenants),
            "카테고리분포": dict(dist),
            "입점업체": tenants,
        }
