"""건물 입점업체(테넌트) 로스터 수집 — Kakao Local 장소 검색 기반.

특정 건물(도로명주소+좌표)에 입점한 매장/업체 목록을 카카오 장소 검색으로
모은다. 단일 키워드 검색은 15건 cap + 카테고리 편향이 있어, 세 경로를 합쳐
디덥한다:
    1. 키워드=건물명 (있으면)        — 빌딩·소매·서비스 폭넓게
    2. 키워드=도로명주소             — 1과 다른 슬라이스 보강
    3. 카테고리 반경 스윕(FD6/CE7/HP8 등) — 음식점·카페·병원 완성

병합 후 place_id로 디덥하고, road_address_name이 대상 건물 도로명과 일치하는
건만 남긴다(반경 내 옆 건물 제거). 리뷰/평점은 이 단계에 없다 — place_id만
확보하고, 별도 스크래퍼(KakaoPlaceScraper)가 후속 보강한다.

반환 dict 키는 한글 (cf. real_estate/repository/commercial_area.py 관례).
"""

from __future__ import annotations

import logging
import re

from real_estate.external.kakao_local import KakaoLocalClient

logger = logging.getLogger("repository.building_tenants")

# 로스터 완성을 위해 스윕할 카테고리 그룹 코드 (키워드가 놓치는 것 위주).
#   FD6 음식점 / CE7 카페 / HP8 병원 / PM9 약국 / BK9 은행 / CS2 편의점
_DEFAULT_SWEEP_CODES = ("FD6", "CE7", "HP8", "PM9", "BK9", "CS2")


def _norm_road(road: str | None) -> str:
    """도로명주소 정규화 — 공백/'지하' 제거 후 '시도 구 도로 번호'만 비교용."""
    if not road:
        return ""
    s = re.sub(r"\s+", "", road)
    s = s.replace("지하", "")
    return s


class BuildingTenantsRepository:
    """Kakao Local로 한 건물의 입점업체 로스터를 수집한다.

    사용법:
        repo = BuildingTenantsRepository(KakaoLocalClient(rest_key))
        tenants = repo.fetch(road_address="서울 강서구 강서로 385",
                             x=126.838, y=37.560, building_name="우성에스비타워")

    fetch() 반환: 테넌트 dict 리스트. 각 dict:
        {
            "place_id": str,        # 카카오 장소 ID (place.map.kakao.com/{id})
            "상호": str,
            "카테고리": str,         # 전체 경로 (예: "음식점 > 한식 > 육류,고기")
            "카테고리그룹": str,      # FD6/CE7/... (없으면 "")
            "도로명주소": str,
            "위도": float, "경도": float,
            "전화": str,
        }
    """

    def __init__(self, kakao_client: KakaoLocalClient):
        self._kakao = kakao_client

    def fetch(self, *, road_address: str, x: float | None = None,
              y: float | None = None, building_name: str | None = None,
              radius: int = 50, max_pages: int = 2,
              sweep_codes: tuple[str, ...] = _DEFAULT_SWEEP_CODES) -> list[dict]:
        """건물 입점업체 로스터를 수집해 반환한다.

        Args:
            road_address:  대상 건물 도로명주소 (필터 기준).
            x, y:          건물 좌표(경도, 위도). 좌표 우선 정렬·카테고리 스윕에 사용.
            building_name: 건물명 (있으면 키워드 검색에 추가).
            radius:        좌표 검색 반경(m). 옆 건물은 도로명 필터로 제거.
            max_pages:     키워드 검색당 최대 페이지(페이지당 15건).
            sweep_codes:   카테고리 반경 스윕 코드 (좌표 있을 때만).

        Returns:
            place_id로 디덥된 테넌트 dict 리스트.
        """
        target = _norm_road(road_address)
        by_id: dict[str, dict] = {}

        def ingest(docs: list[dict]) -> None:
            for d in docs:
                ra = d.get("road_address_name") or ""
                # 도로명 정확 매칭 — 반경 내 옆 건물 배제.
                if target and not _norm_road(ra).startswith(target):
                    continue
                pid = (d.get("place_url") or "").rsplit("/", 1)[-1]
                if not pid or pid in by_id:
                    continue
                by_id[pid] = {
                    "place_id": pid,
                    "상호": d.get("place_name", ""),
                    "카테고리": d.get("category_name", ""),
                    "카테고리그룹": d.get("category_group_code", ""),
                    "도로명주소": ra,
                    "위도": float(d["y"]) if d.get("y") else None,
                    "경도": float(d["x"]) if d.get("x") else None,
                    "전화": d.get("phone", ""),
                }

        # 1) 키워드 검색 (건물명, 도로명) — 페이지네이션.
        queries = [q for q in (building_name, road_address) if q]
        for q in queries:
            for page in range(1, max_pages + 1):
                resp = self._kakao.search_keyword(
                    q, x=x, y=y, radius=radius if (x and y) else None, page=page)
                docs = resp.get("documents", [])
                ingest(docs)
                if resp.get("meta", {}).get("is_end", True) or not docs:
                    break

        # 2) 카테고리 반경 스윕 (좌표 있을 때) — 키워드가 놓친 음식점/카페/병원 완성.
        if x is not None and y is not None:
            for code in sweep_codes:
                resp = self._kakao.search_category(code, x=x, y=y, radius=radius)
                ingest(resp.get("documents", []))

        tenants = list(by_id.values())
        logger.info("building_tenants: %s → %d개 (road=%s)",
                    building_name or road_address, len(tenants), road_address)
        return tenants
