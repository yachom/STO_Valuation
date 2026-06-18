"""건물 임차 회전율(turnover) — LocalData 인허가 이력 기반.

도로명주소로 업종별 인허가/폐업 이력을 모아 같은 건물의 임차 변동을 정량화한다.
'임차안정성' 축의 핵심 정량 근거 — 누적 폐업률, 평균/중앙 존속기간, 최근 회전
속도, 신규 진입, 업태 분포. REB(지역 시장환경)와 달리 **이 건물 자체** 데이터.

업종은 data.go.kr 활용신청별로 늘어난다(미신청 업종은 403 → graceful skip).
서비스명(엔드포인트 path)은 각 데이터셋 openapi 페이지의 Base URL에서 확인.
"""

from __future__ import annotations

import datetime as _dt
import logging
import statistics
from collections import Counter

from infra.external.localdata.client import LocalDataClient, LocalDataError
from real_estate.location import core_road_address as _core_road

logger = logging.getLogger("repository.building_turnover")

# 업종 라벨 → apis.data.go.kr/1741000 서비스 path.
# ✅=활용신청·검증, ○=path 확인(신청 시 활성). 신청 후 주석 갱신.
SERVICE_CATALOG: dict[str, str] = {
    "일반음식점": "general_restaurants",   # ✅
    "휴게음식점": "rest_cafes",            # ○ (신청 시 활성)
    # 신청 후 path 확인하여 추가:
    # "제과점": "...", "의원": "...", "병원": "...", "약국": "...",
    # "안경업": "...", "미용업": "...", "통신판매업": "...",
}


class BuildingTurnoverRepository:
    """LocalData로 건물 임차 회전율 지표를 산출한다."""

    def __init__(self, client: LocalDataClient, catalog: dict | None = None):
        self._c = client
        self._catalog = catalog or SERVICE_CATALOG

    def licenses(self, road_address: str, *, services: list[str] | None = None,
                 max_rows: int = 500) -> tuple[list, dict[str, int], list[str]]:
        """도로명주소의 인허가 이력(영업+폐업)을 업종 API들에서 모아 dedup 반환.

        turnover() 집계와 앵커 스코어링(개별 면적·입점일)의 공용 원천 — 한 번
        조회로 둘을 모두 채운다. 업종은 catalog가 결정하므로 SERVICE_CATALOG에
        데이터셋을 추가하면 자동으로 합산 범위가 넓어진다(코드 변경 불필요).

        Returns:
            (licenses[BusinessLicense], by_service{라벨:건수}, skipped[미신청 라벨])
        """
        road = _core_road(road_address)        # 시/도·시군구 접두 제거 (LIKE 매칭)
        labels = services or list(self._catalog)
        all_lic = []
        by_service: dict[str, int] = {}
        skipped: list[str] = []
        for label in labels:
            svc = self._catalog.get(label)
            if not svc:
                continue
            try:
                lic = self._c.fetch_by_road(svc, road, max_rows=max_rows)
            except LocalDataError as e:
                # 403 = 미신청 업종 → skip (나머지는 진행)
                logger.info("turnover %s skip: %s", label, str(e)[:80])
                skipped.append(label)
                continue
            by_service[label] = len(lic)
            all_lic.extend(lic)

        # 관리번호 dedup
        seen = set()
        deduped = []
        for x in all_lic:
            k = x.관리번호 or f"{x.상호}|{x.인허가일자}"
            if k not in seen:
                seen.add(k)
                deduped.append(x)
        return deduped, by_service, skipped

    def turnover(self, road_address: str, *, services: list[str] | None = None,
                 recent_years: int = 2, today: _dt.date | None = None,
                 max_rows: int = 500) -> dict:
        """도로명주소의 임차 회전율 지표 (licenses() 원천 → 집계)."""
        lic, by_service, skipped = self.licenses(
            road_address, services=services, max_rows=max_rows)
        return self.summarize(lic, by_service, skipped, road_address=road_address,
                              recent_years=recent_years, today=today)

    def summarize(self, lic: list, by_service: dict[str, int] | None = None,
                  skipped: list[str] | None = None, *, road_address: str = "",
                  recent_years: int = 2, today: _dt.date | None = None) -> dict:
        """인허가 리스트 → 회전율 집계 dict.

        Returns:
            {입점이력, 영업중, 폐업, 누적폐업률, 최근폐업, 신규진입,
             평균존속연수, 중앙존속연수, 영업중평균영업연수, 업태분포,
             업종별, 수집업종, 미수집업종}
        """
        today = today or _dt.date.today()
        by_service = by_service or {}
        skipped = skipped or []

        if not lic:
            return {"입점이력": 0, "수집업종": list(by_service), "미수집업종": skipped,
                    "note": "수집된 인허가 이력 없음"}

        closed = [x for x in lic if x.is_closed]
        active = [x for x in lic if not x.is_closed]
        spans = [x.존속연수 for x in closed if x.존속연수 is not None]
        cutoff = _dt.date(today.year - recent_years, today.month, today.day)
        recent_closed = [x for x in closed if x.폐업일자 and x.폐업일자 >= cutoff]
        recent_open = [x for x in lic if x.인허가일자 and x.인허가일자 >= cutoff]
        active_spans = [round((today - x.인허가일자).days / 365.25, 1)
                        for x in active if x.인허가일자]

        return {
            "도로명주소": road_address,
            "입점이력": len(lic),
            "영업중": len(active),
            "폐업": len(closed),
            "누적폐업률": round(len(closed) / len(lic), 3),
            f"최근{recent_years}년_폐업": len(recent_closed),
            f"최근{recent_years}년_신규": len(recent_open),
            "평균존속연수": round(statistics.mean(spans), 1) if spans else None,
            "중앙존속연수": round(statistics.median(spans), 1) if spans else None,
            "영업중_평균영업연수": round(statistics.mean(active_spans), 1) if active_spans else None,
            "업태분포": dict(Counter(x.업태 for x in lic if x.업태).most_common(8)),
            "업종별": by_service,
            "수집업종": list(by_service),
            "미수집업종": skipped,
        }
