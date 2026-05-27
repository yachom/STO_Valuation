"""V-World 토지정보 조회.

V-World(국토교통부 공간정보 오픈플랫폼)에서 한 필지의 토지 정보를 조회한다.
한 번의 fetch() 호출로 네 가지 정보를 모두 가져온다:

    - 토지대장      : 지목, 토지면적
    - 토지특성      : 용도지역, 도로조건, 토지이용상황, 지형
    - 토지이용계획   : 용도지역/지구/구역 + 규제 (토지거래허가구역 등)
    - 개별공시지가   : 연도별 공시지가

이 중 '토지이용계획'은 이상 신호 조사 단계에서 특히 중요하다.
토지거래허가구역 같은 규제는 STO 발행 구조에 영향을 줄 수 있다.

조회 키 — PNU:
    V-World 토지 API는 PNU(19자리 필지 고유번호)로 조회한다.
    LocationInfo에는 PNU 필드가 없지만 다음 조각들로 조립할 수 있다:
        PNU = 법정동코드(10) + 산여부(1) + 본번(4) + 부번(4)
            = location.bcode + "1" + bun.zfill(4) + ji.zfill(4)
    산여부는 일반 토지면 "1", 산이면 "2". 대부분의 상가건물은 일반 토지라
    "1"로 둔다.

반환 dict의 키:
    이 시스템의 데이터 소스들은 반환 dict의 키를 한글로 쓴다. 최종적으로
    LLM 프롬프트에 들어가므로 한글 키가 그대로 읽히고, 키 자체가 무슨
    뜻인지 바로 보인다.

호출 방식:
    V-World 데이터 API를 requests로 직접 호출한다. 네 정보는 각각
    별도의 data 종류로 조회하며 요청 형식은 거의 동일하다.

API 키:
    V-World(vworld.kr)에서 발급받는 인증키. 생성자에서 받는다.

주의 — 서비스 ID:
    아래 _DATA_* 상수 중 연속지적도(LP_PA_CBND_BUBUN)는 V-World 문서에서
    확인된 값이다. 토지특성·토지이용계획·공시지가의 서비스 ID는 V-World
    개발자센터의 '2D 데이터 API' 데이터 목록에서 정확한 값을 확인해야 한다.
    실제 키로 테스트할 때 응답이 비어있다면 이 ID부터 점검할 것.
"""

import json

import requests

from STO_Valuation.real_estate.location import LocationInfo
from STO_Valuation.real_estate.repository.base import BaseRepository


class LandInfoRepository(BaseRepository):
    """V-World 토지정보 조회 데이터 소스.

    사용법:
        repo = LandInfoRepository(vworld_api_key="V-World_인증키")
        land = repo.fetch(location)

    fetch() 반환 dict 스키마:
        {
            "토지대장": {
                # 필지 기본 정보. 못 찾으면 값이 전부 None.
                "지목": str | None,         # 예: "대"
                "토지면적": float | None,    # ㎡
            },

            "토지특성": {
                # 가장 최근 연도 1건. 못 찾으면 값이 전부 None.
                "용도지역": str | None,      # 예: "제2종일반주거지역"
                "토지이용상황": str | None,  # 예: "주상용"
                "도로조건": str | None,      # 예: "소로각지"
                "지형고저": str | None,      # 예: "평지"
                "지형형상": str | None,      # 예: "부정형"
                "공시지가": float | None,    # 해당 연도 공시지가 (원/㎡)
                "기준연도": str | None,      # 예: "2022"
            },

            "토지이용계획": [
                # 필지에 걸린 용도지역/지구/구역 목록. 규제 항목
                # (토지거래허가구역 등)이 모두 들어온다. 없으면 빈 리스트.
                {
                    "지역지구구역명": str | None,
                                # 예: "토지거래계약에관한허가구역"
                    "저촉여부": str | None,  # 예: "포함", "접함"
                },
                # ...
            ],

            "공시지가이력": [
                # 연도별 개별공시지가 시계열. 최근 연도 순.
                # 없으면 빈 리스트.
                {
                    "연도": str | None,
                    "공시지가": float | None,  # 원/㎡
                },
                # ...
            ],
        }

    주의:
        - "토지대장", "토지특성"은 항상 dict (못 찾으면 값이 전부 None).
        - "토지이용계획", "공시지가이력"은 항상 리스트 (없으면 빈 리스트).
        - "토지특성"은 V-World가 연도별로 여러 건 주지만, 가장 최근 1건만
          담는다. 공시지가 추이는 "공시지가이력"에서 따로 본다.
    """

    # V-World 데이터 API 엔드포인트 (네 정보 공통, data 파라미터로 구분).
    ENDPOINT = "https://api.vworld.kr/req/data"

    # 각 정보별 V-World data 종류(서비스 ID).
    # LP_PA_CBND_BUBUN(연속지적도)만 문서에서 확인된 값. 나머지 셋은
    # V-World 데이터 목록에서 정확한 ID 확인 필요 (위 모듈 docstring 참고).
    _DATA_LAND_LEDGER = "LP_PA_CBND_BUBUN"      # 연속지적도(필지)
    _DATA_CHARACTERISTICS = "A2SM_LANDCHRCTR"   # 토지특성 (※ ID 확인 필요)
    _DATA_USE_PLAN = "LT_C_LANDUSE"             # 토지이용계획 (※ ID 확인 필요)
    _DATA_LAND_PRICE = "A2SM_LANDPRCEHS"        # 개별공시지가 (※ ID 확인 필요)

    def __init__(self, vworld_api_key):
        """데이터 소스 인스턴스를 만든다.

        Args:
            vworld_api_key: V-World(vworld.kr)에서 발급받은 인증키.
        """
        self._api_key = vworld_api_key

    def fetch(self, location: LocationInfo) -> dict:
        """LocationInfo를 받아 V-World 토지정보를 dict로 반환한다.

        Args:
            location: 정규화된 위치 정보. bcode, bun, ji로 PNU를 조립한다.

        Returns:
            토지정보 dict (클래스 docstring의 스키마 참고). 키는 한글.

        Raises:
            ValueError:   location에 bcode 또는 bun이 없는 경우.
            RuntimeError: API 응답이 비정상인 경우.
        """
        # 1) PNU 조립.
        pnu = self._build_pnu(location)

        # 2) 네 종류의 토지 정보를 각각 조회.
        ledger_rows = self._call_api(self._DATA_LAND_LEDGER, pnu)
        char_rows = self._call_api(self._DATA_CHARACTERISTICS, pnu)
        useplan_rows = self._call_api(self._DATA_USE_PLAN, pnu)
        price_rows = self._call_api(self._DATA_LAND_PRICE, pnu)

        # 3) 각각 우리 스키마(한글 키)로 변환.
        return {
            "토지대장": self._build_ledger(ledger_rows),
            "토지특성": self._build_characteristics(char_rows),
            "토지이용계획": self._build_use_plan(useplan_rows),
            "공시지가이력": self._build_price_history(price_rows),
        }

    def _build_pnu(self, location):
        """LocationInfo의 조각들로 PNU 19자리를 조립한다 (내부 메서드).

        PNU = 법정동코드(10) + 산여부(1) + 본번(4) + 부번(4)

        Args:
            location: LocationInfo.

        Returns:
            PNU 문자열 (19자리).

        Raises:
            ValueError: bcode 또는 bun이 없는 경우.
        """
        if location.bcode is None or location.bun is None:
            raise ValueError("LandInfoRepository는 bcode와 bun이 필요합니다.")

        bjd_code = location.bcode                       # 법정동코드 10자리
        mountain = "1"                                  # 산여부: 일반 토지 = "1"
        bun = str(location.bun).zfill(4)                # 본번 4자리
        ji = str(location.ji).zfill(4) if location.ji is not None else "0000"

        return bjd_code + mountain + bun + ji

    def _call_api(self, data_name, pnu):
        """V-World 데이터 API를 호출하고 결과 row 리스트를 반환한다 (내부 메서드).

        네 종류 모두 요청 형식이 같아 이 메서드를 공유한다. data 종류만
        인자로 받는다.

        Args:
            data_name: V-World data 종류(서비스 ID).
            pnu:       조회할 필지의 PNU.

        Returns:
            결과 feature(행)의 properties 리스트. 결과가 없으면 빈 리스트.

        Raises:
            RuntimeError: HTTP 오류, JSON 파싱 실패, API 비정상 응답인 경우.
        """
        params = {
            "service": "data",
            "request": "GetFeature",
            "data": data_name,
            "key": self._api_key,
            "format": "json",
            "attrFilter": "pnu:=:" + pnu,    # PNU가 일치하는 필지만
            "size": "100",
        }

        # 호출.
        try:
            response = requests.get(self.ENDPOINT, params=params, timeout=10)
            response_body = response.text
        except Exception as e:
            raise RuntimeError("V-World API 호출 실패: " + str(e))

        # JSON 파싱.
        try:
            data = json.loads(response_body)
        except json.JSONDecodeError:
            raise RuntimeError(
                "V-World API가 JSON이 아닌 응답을 반환했습니다. "
                "(키 오류 또는 서버 오류 가능) 응답 원문: "
                + response_body[:500]
            )

        # V-World 응답 상태 확인.
        # 정상이면 response.status == "OK". 결과 없으면 "NOT_FOUND".
        resp = data.get("response", {})
        status = resp.get("status")

        if status == "NOT_FOUND":
            return []
        if status != "OK":
            # ERROR 등. 에러 메시지를 담아 던진다.
            error = resp.get("error", {})
            raise RuntimeError(
                "V-World API 오류: " + str(error.get("text", status))
            )

        # 결과 feature 리스트 추출.
        # 구조: response.result.featureCollection.features[].properties
        result = resp.get("result", {})
        feature_collection = result.get("featureCollection", {})
        features = feature_collection.get("features", [])

        # 각 feature의 properties만 뽑아 리스트로 반환.
        rows = []
        for feature in features:
            props = feature.get("properties", {})
            if props:
                rows.append(props)
        return rows

    # ── 토지대장 변환 ─────────────────────────────────────────────

    def _build_ledger(self, rows) -> dict:
        """토지대장 row 리스트를 단일 dict로 변환한다 (내부 메서드).

        첫 행만 사용한다. 결과가 없으면 값이 전부 None.
        """
        if not rows:
            return {"지목": None, "토지면적": None}

        row = rows[0]
        return {
            "지목": self._str(row.get("lndcgrCodeNm")),
            "토지면적": self._float(row.get("lndpclAr")),
        }

    # ── 토지특성 변환 ─────────────────────────────────────────────

    def _build_characteristics(self, rows) -> dict:
        """토지특성 row 리스트를 단일 dict로 변환한다 (내부 메서드).

        V-World는 연도별로 여러 건을 주므로, 기준연도가 가장 최근인
        1건을 골라 변환한다. 결과가 없으면 값이 전부 None.
        """
        empty = {
            "용도지역": None, "토지이용상황": None,
            "도로조건": None, "지형고저": None,
            "지형형상": None, "공시지가": None, "기준연도": None,
        }
        if not rows:
            return empty

        # 기준연도(stdrYear)가 가장 큰(최근) 행을 고른다.
        latest = None
        latest_year = -1
        for row in rows:
            try:
                year = int(row.get("stdrYear"))
            except (ValueError, TypeError):
                year = -1
            if year > latest_year:
                latest_year = year
                latest = row

        if latest is None:
            return empty

        return {
            "용도지역": self._str(latest.get("prposArea1Nm")),
            "토지이용상황": self._str(latest.get("ladUseSittnNm")),
            "도로조건": self._str(latest.get("roadSideCodeNm")),
            "지형고저": self._str(latest.get("tpgrphHgCodeNm")),
            "지형형상": self._str(latest.get("tpgrphFrmCodeNm")),
            "공시지가": self._float(latest.get("pblntfPclnd")),
            "기준연도": self._str(latest.get("stdrYear")),
        }

    # ── 토지이용계획 변환 ─────────────────────────────────────────

    def _build_use_plan(self, rows) -> list:
        """토지이용계획 row 리스트를 dict 리스트로 변환한다 (내부 메서드).

        필지에 걸린 용도지역/지구/구역마다 dict 하나. 규제 항목
        (토지거래허가구역 등)이 모두 들어온다. 없으면 빈 리스트.
        """
        use_plan = []
        for row in rows:
            use_plan.append({
                "지역지구구역명": self._str(row.get("prposAreaDstrcCodeNm")),
                "저촉여부": self._str(row.get("cnflcAtNm")),
            })
        return use_plan

    # ── 개별공시지가 변환 ─────────────────────────────────────────

    def _build_price_history(self, rows) -> list:
        """개별공시지가 row 리스트를 dict 리스트로 변환한다 (내부 메서드).

        연도별 공시지가. 최근 연도 순으로 정렬해 반환한다.
        없으면 빈 리스트.
        """
        history = []
        for row in rows:
            year = self._str(row.get("stdrYear"))
            price = self._float(row.get("pblntfPclnd"))
            if year is None and price is None:
                continue
            history.append({"연도": year, "공시지가": price})

        # 연도 내림차순 정렬 (최근이 앞으로).
        history.sort(
            key=lambda h: h["연도"] if h["연도"] is not None else "",
            reverse=True,
        )
        return history

    # ── 공통 변환 헬퍼 ────────────────────────────────────────────

    def _str(self, value):
        """값을 문자열로 정리한다. 빈 값이면 None (내부 메서드)."""
        if value is None:
            return None
        text = str(value).strip()
        if text == "":
            return None
        return text

    def _float(self, value):
        """값을 실수로 변환한다. 실패하면 None (내부 메서드)."""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
