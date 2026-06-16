"""상권 업종별 추정매출 — 부동산 전용 graphflow 도구.

real_estate/repository의 CommercialSalesRepository(SQLite 조회)를 BaseTool로
감싸 트레이싱을 자동으로 얻는다. commercial(상권·입지 활성도) 축에 분기 단위
업종 매출 실수치를 공급한다 — 뉴스 일화가 아닌 정량 grounding.

입력은 trdar_cd(상권코드)가 1차다. 평가 대상의 상권코드는 seed 단계에서 이미
Neo4j(CommercialArea 노드)에 적재돼 있으므로, 보고서 흐름에서는 그래프에서 읽어
넘긴다. 좌표만 있으면 location으로 상권을 해석한다.
"""

from __future__ import annotations

import logging

from infra.tool.base import BaseTool
from real_estate.location import LocationInfo
from real_estate.repository.commercial_sales import CommercialSalesRepository

logger = logging.getLogger("graphflow.tool.commercial_sales")


class CommercialSalesTool(BaseTool):
    """상권코드(또는 좌표) → 업종별 추정매출 top_n. commercial 축 정량 근거."""

    name = "commercial_sales"

    def __init__(self, db_path: str):
        super().__init__()
        self._repo = CommercialSalesRepository(db_path)

    def _run(self, *, trdar_cds: list[str] | None = None,
             location: LocationInfo | None = None, top_n: int = 10):
        """Args:
            trdar_cds: 상권코드 리스트 (그래프에서 읽어 넘김 — 1차 경로).
            location:  좌표 폴백 (trdar_cds 없을 때 상권 해석).
            top_n:     상권별 상위 업종 수.

        Returns:
            {"매칭상권수": int, "상권목록": [업종매출 dict, ...]}
        """
        if trdar_cds:
            areas = [self._repo.top_industries(cd, top_n=top_n) for cd in trdar_cds]
            areas = [a for a in areas if a is not None]
            return {"매칭상권수": len(areas), "상권목록": areas}
        if location is not None:
            return self._repo.fetch(location, top_n=top_n)
        return {"매칭상권수": 0, "상권목록": [],
                "error": "trdar_cds 또는 location 중 하나가 필요합니다."}
