"""반경 내 배후 아파트 세대수 조회 (간단 버전).

real_estate.db의 complex_master 테이블을 읽어, 건물 주변(반경 내)
아파트 단지 수와 총 세대수를 조회한다. 시세는 보지 않고 세대수만 본다.

용도:
    상가건물의 배후 주거수요 규모를 세대수로 가늠한다.
    "이 건물 반경 500m 안에 아파트 N단지, 총 M세대가 산다" 수준.
"""

import logging
import math

from real_estate.location import LocationInfo
from real_estate.repository._db import connection

logger = logging.getLogger("repository.nearby_apartment")


class NearbyApartmentRepository:
    """반경 내 배후 아파트 세대수 조회 데이터 소스.

    사용법:
        repo = NearbyApartmentRepository(
            db_path="db/real_estate.db",
            radius_m=500,
        )
        result = repo.fetch(location)

    fetch() 반환 dict 스키마:
        {
            "반경m": int,        # 생성자에서 받은 반경
            "단지수": int,       # 반경 내 아파트 단지 수
            "총세대수": int,     # 반경 내 모든 단지 세대수 합
            "단지목록": [        # 거리 가까운 순
                {"단지명": str, "거리m": int, "세대수": int | None},
                # ...
            ],
        }
    """

    _METERS_PER_DEG_LAT = 111_000   # 위도 1도 ≈ 111km (bbox 1차 필터용)
    _EARTH_R = 6_371_000

    def __init__(self, db_path, radius_m=500):
        """데이터 소스 인스턴스를 만든다.

        Args:
            db_path:  real_estate.db 파일 경로.
            radius_m: 배후 단지를 모을 반경(미터). 기본 500m.
        """
        self._db_path = db_path
        self._radius_m = radius_m

    def fetch(self, location: LocationInfo) -> dict:
        """LocationInfo를 받아 반경 내 아파트 세대수를 dict로 반환한다.

        Args:
            location: 정규화된 위치 정보. latitude, longitude를 사용한다.

        Returns:
            세대수 정보 dict (클래스 docstring의 스키마 참고).

        Raises:
            ValueError: location에 좌표가 없는 경우.
        """
        if location.latitude is None or location.longitude is None:
            raise ValueError(
                "NearbyApartmentRepository는 위도·경도가 필요합니다."
            )

        lat = location.latitude
        lon = location.longitude

        with connection(self._db_path) as conn:
            complexes = self._find_complexes(conn, lat, lon)

        total_households = sum(c["세대수"] for c in complexes if c["세대수"])

        return {
            "반경m": self._radius_m,
            "단지수": len(complexes),
            "총세대수": total_households,
            "단지목록": complexes,
        }

    def _find_complexes(self, conn, lat, lon) -> list:
        """반경 내 아파트 단지를 거리순으로 찾는다 (내부 메서드).

        bbox 1차 필터 -> haversine 정밀 거리 2단계로 판정한다.

        Args:
            conn:     DB 연결.
            lat, lon: 건물 좌표.

        Returns:
            단지 dict 리스트 (거리 가까운 순).
        """
        cur = conn.cursor()

        # 1차: bbox 필터
        delta_lat = self._radius_m / self._METERS_PER_DEG_LAT
        delta_lon = self._radius_m / (
            self._METERS_PER_DEG_LAT * math.cos(math.radians(lat))
        )

        cur.execute("""
            SELECT canonical_name, latitude, longitude, household_count
            FROM apartment_complex
            WHERE latitude  BETWEEN ? AND ?
              AND longitude BETWEEN ? AND ?
        """, (lat - delta_lat, lat + delta_lat,
              lon - delta_lon, lon + delta_lon))
        candidates = cur.fetchall()

        # 2차: haversine 정밀 거리 -> 반경 안만
        matched = []
        for name, c_lat, c_lon, households in candidates:
            dist = self._haversine_m(lat, lon, c_lat, c_lon)
            if dist > self._radius_m:
                continue
            matched.append({
                "단지명": name,
                "거리m": int(round(dist)),
                "세대수": households,
            })

        matched.sort(key=lambda c: c["거리m"])
        return matched

    def _haversine_m(self, lat1, lon1, lat2, lon2) -> float:
        """두 좌표 간 거리(미터). haversine 공식 (내부 메서드)."""
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = (math.sin(dp / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
        return 2 * self._EARTH_R * math.asin(math.sqrt(a))
