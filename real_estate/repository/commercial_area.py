"""상권 기반 인구 정보 조회.

서울시 상권분석서비스 데이터로 만든 real_estate.db를 읽어,
건물이 속한 상권의 유동인구·거주인구를 조회한다.

조회 방식:
    건물 좌표(위경도)가 어느 상권 영역 안에 있는지 point-in-polygon으로
    판정한다. 상권 영역은 서로 겹칠 수 있으므로, 건물이 여러 상권에
    동시에 속할 수 있다. 이 경우 겹친 상권을 모두 반환한다(하나로 줄이지
    않는다). 평가 단계의 LLM이 종합 판단한다.

    어느 상권에도 속하지 않는 건물도 있다(상권은 서울 전역을 빈틈없이
    덮지 않는다). 이 경우 행정동 단위로 폴백한다. 행정동은 서울 전역을
    빈틈없이 분할하므로 누락이 없다. 폴백 시 반환 dict의 "매칭수준"이
    "행정동(폴백)"이 되어, 평가 단계의 LLM이 이 데이터가 상권보다 거친
    추정임을 인식할 수 있다.

데이터 출처:
    real_estate.db — build_commercial_area_db.ipynb로 미리 만들어 둔
    SQLite DB. 다섯 테이블로 구성:
        - commercial_area          : 상권 영역(경계 다각형)
        - floating_population      : 분기별 유동인구 (상권 단위)
        - resident_population      : 분기별 거주인구 (상권 단위)
        - floating_population_dong : 분기별 유동인구 (행정동 단위, 폴백용)
        - resident_population_dong : 분기별 거주인구 (행정동 단위, 폴백용)

반환 dict의 키:
    이 시스템의 데이터 소스들은 반환 dict의 키를 한글로 쓴다.
    (DB 내부 컬럼명은 영문이지만, fetch가 한글 키로 변환해 반환한다.)
"""

import json
import logging
import sqlite3

from real_estate.location import LocationInfo

logger = logging.getLogger("repository.commercial_area")


class CommercialAreaRepository:
    """상권 기반 유동인구·거주인구 조회 데이터 소스.

    사용법:
        repo = CommercialAreaRepository(db_path="db/real_estate.db")
        result = repo.fetch(location)

    fetch() 반환 dict 스키마:
        {
            "매칭수준": str,
                # "상권"        — 상권 영역에 매칭됨 (정상)
                # "행정동(폴백)" — 어느 상권에도 안 속해 행정동 단위로
                #                 조회함. 상권보다 거친 추정치.
                # "매칭없음"     — 상권도 행정동도 조회 실패
                #                 (hcode 없음 등 예외적 경우)
            "매칭상권수": int,
                # 건물이 속한 상권 개수. 0이면 상권 매칭 실패
                # (행정동 폴백 또는 매칭없음), 2 이상이면 겹침.

            "상권목록": [
                # 건물이 속한 상권마다 dict 하나. 겹치면 여러 개.
                # 행정동 폴백인 경우 행정동 1건이 담긴다.
                # 매칭없음인 경우 빈 리스트.
                #
                # 행정동 폴백 항목은 상권 고유 필드가 비어있다:
                #   상권코드=None, 상권구분="행정동", 상권면적=None.
                {
                    "상권코드": str,
                    "상권명": str,
                    "상권구분": str,        # 골목상권/발달상권/전통시장/관광특구
                    "행정동코드": str,
                    "행정동명": str,
                    "상권면적": float,      # ㎡

                    "유동인구": [
                        # 최근 분기들의 유동인구. 최신 분기가 앞.
                        # 각 분기 dict는 전체 항목(성별·연령대·시간대·요일).
                        {
                            "기준분기": str,       # 예: "20254"
                            "총": int,
                            "남성": int, "여성": int,
                            "연령_10": int, "연령_20": int, "연령_30": int,
                            "연령_40": int, "연령_50": int, "연령_60이상": int,
                            "시간_00_06": int, "시간_06_11": int,
                            "시간_11_14": int, "시간_14_17": int,
                            "시간_17_21": int, "시간_21_24": int,
                            "월": int, "화": int, "수": int, "목": int,
                            "금": int, "토": int, "일": int,
                        },
                        # ...
                    ],

                    "거주인구": [
                        # 최근 분기들의 거주인구. 최신 분기가 앞.
                        {
                            "기준분기": str,
                            "총": int,
                            "남성": int, "여성": int,
                            "연령_10": int, "연령_20": int, "연령_30": int,
                            "연령_40": int, "연령_50": int, "연령_60이상": int,
                            "남성_10": int, "남성_20": int, "남성_30": int,
                            "남성_40": int, "남성_50": int, "남성_60이상": int,
                            "여성_10": int, "여성_20": int, "여성_30": int,
                            "여성_40": int, "여성_50": int, "여성_60이상": int,
                            "총가구수": int,
                            "아파트가구수": int, "비아파트가구수": int,
                        },
                        # ...
                    ],
                },
                # ...
            ],
        }

    주의:
        - "상권목록"은 항상 리스트 (매칭 없으면 빈 리스트).
        - 유동인구·거주인구도 항상 리스트. 분기 데이터가 없으면 빈 리스트.
        - 한 상권의 유동인구는 있는데 거주인구가 없을 수 있다
          (거주인구 데이터는 일부 상권이 누락됨).
    """

    # fetch가 반환할 최근 분기 수.
    RECENT_QUARTERS = 8

    # ── 유동인구: DB 영문 컬럼 → 한글 키 ──────────────────────────
    _FLOATING_MAP = {
        "quarter_code": "기준분기",
        "total": "총",
        "male": "남성", "female": "여성",
        "age_10": "연령_10", "age_20": "연령_20", "age_30": "연령_30",
        "age_40": "연령_40", "age_50": "연령_50", "age_60_over": "연령_60이상",
        "time_00_06": "시간_00_06", "time_06_11": "시간_06_11",
        "time_11_14": "시간_11_14", "time_14_17": "시간_14_17",
        "time_17_21": "시간_17_21", "time_21_24": "시간_21_24",
        "mon": "월", "tue": "화", "wed": "수", "thu": "목",
        "fri": "금", "sat": "토", "sun": "일",
    }

    # ── 거주인구: DB 영문 컬럼 → 한글 키 ──────────────────────────
    _RESIDENT_MAP = {
        "quarter_code": "기준분기",
        "total": "총",
        "male": "남성", "female": "여성",
        "age_10": "연령_10", "age_20": "연령_20", "age_30": "연령_30",
        "age_40": "연령_40", "age_50": "연령_50", "age_60_over": "연령_60이상",
        "male_10": "남성_10", "male_20": "남성_20", "male_30": "남성_30",
        "male_40": "남성_40", "male_50": "남성_50", "male_60_over": "남성_60이상",
        "female_10": "여성_10", "female_20": "여성_20", "female_30": "여성_30",
        "female_40": "여성_40", "female_50": "여성_50",
        "female_60_over": "여성_60이상",
        "household": "총가구수",
        "apartment_household": "아파트가구수",
        "non_apartment_household": "비아파트가구수",
    }

    def __init__(self, db_path):
        """데이터 소스 인스턴스를 만든다.

        Args:
            db_path: real_estate.db 파일 경로.
        """
        self._db_path = db_path

    def fetch(self, location: LocationInfo) -> dict:
        """LocationInfo를 받아 상권 기반 인구 정보를 dict로 반환한다.

        Args:
            location: 정규화된 위치 정보. latitude, longitude를 사용한다.

        Returns:
            상권 인구 정보 dict (클래스 docstring의 스키마 참고).

        Raises:
            ValueError: location에 좌표가 없는 경우.
        """
        if location.latitude is None or location.longitude is None:
            raise ValueError(
                "CommercialAreaRepository는 위도·경도가 필요합니다."
            )

        lon = location.longitude
        lat = location.latitude

        # DB 연결. 읽기 전용으로 연다.
        conn = sqlite3.connect(self._db_path)
        try:
            # 1) 건물이 속한 상권들을 찾는다 (겹치면 여러 개).
            matched_areas = self._find_areas(conn, lon, lat)

            # 2) 각 상권의 인구 데이터를 채운다.
            area_list = []
            for area in matched_areas:
                trdar_cd = area["상권코드"]
                area["유동인구"] = self._fetch_population(
                    conn, "commercial_floating_population", self._FLOATING_MAP,
                    "trdar_cd", trdar_cd
                )
                area["거주인구"] = self._fetch_population(
                    conn, "commercial_resident_population", self._RESIDENT_MAP,
                    "trdar_cd", trdar_cd
                )
                area_list.append(area)

            # 3) 상권 매칭 성공 — 상권 단위 결과 반환.
            if area_list:
                return {
                    "매칭수준": "상권",
                    "매칭상권수": len(area_list),
                    "상권목록": area_list,
                }

            # 4) 상권 매칭 실패 — 행정동 단위로 폴백한다.
            #    상권은 서울 전역을 빈틈없이 덮지 않으므로 어느 상권에도
            #    안 속하는 건물이 있다. 이때 행정동 단위 데이터를 쓴다.
            #    행정동은 서울 전역을 빈틈없이 분할하므로 누락이 없다.
            return self._fetch_by_dong(conn, location)
        finally:
            conn.close()

    def _fetch_by_dong(self, conn, location) -> dict:
        """행정동 단위로 인구 정보를 조회한다 (내부 메서드 — 폴백).

        상권 매칭이 실패했을 때 호출된다. LocationInfo의 hcode로 행정동
        단위 유동인구·거주인구를 조회한다.

        hcode는 10자리이고 행정동 테이블의 adstrd_cd는 8자리다. hcode의
        앞 8자리가 adstrd_cd와 일치하므로 잘라서 조회한다.

        반환 dict는 상권 매칭 때와 같은 구조를 유지하되, "매칭수준"이
        "행정동(폴백)"이고 "상권목록"의 항목이 행정동 1건이다. 평가
        단계의 LLM은 "매칭수준"을 보고 이 데이터가 상권보다 거친 행정동
        단위 추정임을 인식할 수 있다.

        Args:
            conn:     DB 연결.
            location: LocationInfo. hcode를 사용한다.

        Returns:
            인구 정보 dict. hcode가 없으면 "매칭수준"이 "매칭없음".
        """
        # hcode가 없으면 폴백조차 불가.
        if location.hcode is None or len(location.hcode) < 8:
            return {
                "매칭수준": "매칭없음",
                "매칭상권수": 0,
                "상권목록": [],
            }

        # hcode 10자리 → 앞 8자리가 행정동 테이블의 adstrd_cd.
        dong_code = location.hcode[:8]

        floating = self._fetch_population(
            conn, "admin_dong_floating_population", self._FLOATING_MAP,
            "adstrd_cd", dong_code
        )
        resident = self._fetch_population(
            conn, "admin_dong_resident_population", self._RESIDENT_MAP,
            "adstrd_cd", dong_code
        )

        # 행정동 데이터가 아예 없으면 매칭없음.
        if not floating and not resident:
            return {
                "매칭수준": "매칭없음",
                "매칭상권수": 0,
                "상권목록": [],
            }

        # 행정동 1건을 상권목록과 같은 형태의 항목으로 담는다.
        # 상권 고유 필드(상권코드·상권구분·상권면적)는 행정동엔 없으므로
        # 행정동 정보로 채우거나 None으로 둔다.
        dong_item = {
            "상권코드": None,
            "상권명": location.hname,
            "상권구분": "행정동",
            "행정동코드": dong_code,
            "행정동명": location.hname,
            "상권면적": None,
            "유동인구": floating,
            "거주인구": resident,
        }

        return {
            "매칭수준": "행정동(폴백)",
            "매칭상권수": 0,        # 상권 매칭이 0이라 폴백한 것이므로 0
            "상권목록": [dong_item],
        }

    # ── 상권 매칭 ─────────────────────────────────────────────────

    def _find_areas(self, conn, lon, lat) -> list:
        """좌표가 속한 상권들을 찾는다 (내부 메서드).

        2단계로 판정한다:
          1차 — 경계상자(bbox) 필터: 좌표가 상권의 사각형 범위 안에 드는
                상권만 후보로 추린다. 빠르다.
          2차 — point-in-polygon: 후보 상권의 실제 다각형 안에 좌표가
                있는지 정밀 판정한다.

        Args:
            conn:     DB 연결.
            lon, lat: 건물 좌표 (경도, 위도).

        Returns:
            매칭된 상권 dict의 리스트. 각 dict는 상권 속성만 담고
            (유동인구·거주인구는 아직 없음), 겹치면 여러 개.
        """
        cur = conn.cursor()

        # 1차: bbox 안에 드는 상권만
        cur.execute("""
            SELECT trdar_cd, trdar_name, trdar_se_name,
                   adstrd_cd, adstrd_name, area_sqm, polygon_json
            FROM commercial_area
            WHERE ? BETWEEN bbox_min_lon AND bbox_max_lon
              AND ? BETWEEN bbox_min_lat AND bbox_max_lat
        """, (lon, lat))
        candidates = cur.fetchall()

        # 2차: 실제 다각형 안에 있는지
        matched = []
        for row in candidates:
            (trdar_cd, trdar_name, se_name,
             adstrd_cd, adstrd_name, area_sqm, polygon_json) = row

            parts = json.loads(polygon_json)
            if not parts:
                continue

            # parts[0]은 외곽선. 그 안에 좌표가 들면 이 상권에 속한다.
            if self._point_in_polygon(lon, lat, parts[0]):
                matched.append({
                    "상권코드": trdar_cd,
                    "상권명": trdar_name,
                    "상권구분": se_name,
                    "행정동코드": adstrd_cd,
                    "행정동명": adstrd_name,
                    "상권면적": area_sqm,
                })
        return matched

    def _point_in_polygon(self, lon, lat, ring) -> bool:
        """좌표가 다각형 안에 있는지 판정한다 (내부 메서드).

        ray casting 알고리즘. 좌표에서 수평으로 반직선을 그어, 다각형의
        변과 홀수 번 교차하면 내부, 짝수 번이면 외부다.

        Args:
            lon, lat: 판정할 좌표.
            ring:     다각형 외곽선. [[lon, lat], ...] 형태.

        Returns:
            내부면 True, 외부면 False.
        """
        inside = False
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            # 변 (i,j)가 좌표의 위도를 가로지르는지 확인하고,
            # 가로지른다면 그 교차점이 좌표보다 오른쪽인지 본다.
            if (yi > lat) != (yj > lat):
                x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
                if lon < x_cross:
                    inside = not inside
            j = i
        return inside

    # ── 인구 데이터 조회 ──────────────────────────────────────────

    def _fetch_population(self, conn, table, field_map,
                          key_column, key_value) -> list:
        """한 지역의 최근 분기 인구 데이터를 조회한다 (내부 메서드).

        상권/행정동, 유동인구/거주인구가 테이블·키 컬럼만 다르고 조회
        방식이 같아 이 메서드를 공유한다. 최신 분기부터 RECENT_QUARTERS개를
        가져온다.

        Args:
            conn:       DB 연결.
            table:      조회할 테이블 이름.
            field_map:  {DB 영문 컬럼: 한글 키} 매핑.
            key_column: 조회 키 컬럼명 ("trdar_cd" 또는 "adstrd_cd").
            key_value:  키 값 (상권코드 또는 행정동코드).

        Returns:
            분기별 인구 dict의 리스트 (최신 분기가 앞).
            데이터가 없으면 빈 리스트.
        """
        cur = conn.cursor()

        # 최신 분기부터 RECENT_QUARTERS개. quarter_code는 "20254" 같은
        # 문자열이라 사전식 정렬이 곧 시간순 정렬이다.
        columns = list(field_map.keys())
        cur.execute(f"""
            SELECT {", ".join(columns)}
            FROM {table}
            WHERE {key_column} = ?
            ORDER BY quarter_code DESC
            LIMIT ?
        """, (key_value, self.RECENT_QUARTERS))
        rows = cur.fetchall()

        # 각 행을 한글 키 dict로 변환.
        result = []
        for row in rows:
            quarter = {}
            for i, col in enumerate(columns):
                quarter[field_map[col]] = row[i]
            result.append(quarter)
        return result
