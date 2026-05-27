"""건축HUB 건축물대장 조회.

건축HUB(건축물대장정보 서비스)에서 한 건물의 정보를 조회한다. 한 번의
fetch() 호출로 세 가지 대장을 모두 가져온다:

    - 표제부      : 건물 전체 정보 (면적·층수·구조·용도·준공일·ESG 등급)
    - 층별개요     : 층별 용도·면적 (1층 소매점, 2층 음식점 ...)
    - 지역지구구역 : 용도지역 (일반상업지역, 제2종일반주거지역 ...)

세 대장 모두 같은 건축HUB API에서, 같은 키(시군구코드+법정동코드+본번+부번)로
호출하므로 한 클래스에 묶는다. 호출하는 쪽은 fetch() 한 번으로 건물 정보를
통째로 받는다.

호출 방식:
    공공데이터포털 건축HUB 엔드포인트를 requests로 직접 호출한다.
    각 대장은 엔드포인트만 다르고 요청 파라미터는 동일하다.

API 키:
    공공데이터포털 '건축HUB_건축물대장정보 서비스'의 일반 인증키(Decoding).
    생성자에서 받는다.
"""

import json

import requests

from STO_Valuation.real_estate.location import LocationInfo
from STO_Valuation.real_estate.repository.base import BaseRepository


class BuildingRegisterRepository(BaseRepository):
    """건축HUB 건축물대장 조회 데이터 소스.

    사용법:
        repo = BuildingRegisterRepository(service_key="공공데이터포털_Decoding_키")
        facts = repo.fetch(location)

    fetch() 반환 dict 스키마 (키는 모두 한글):
        {
            "표제부": {
                # 건물 한 채의 전체 정보 (건축HUB 표제부의 모든 필드).
                # 건물을 못 찾으면 모든 값이 None인 dict.
                #
                # 식별·주소: 지번주소, 도로명주소, 시군구코드, 법정동코드,
                #            대지구분코드, 번, 지, 관리건축물대장PK, 대장구분,
                #            대장종류, 건물명, 특수지명, 블록, 로트, 외필지수,
                #            새주소도로코드, 새주소법정동코드, 새주소지상지하코드,
                #            새주소본번, 새주소부번, 동명칭, 주부속구분
                # 규모:      대지면적, 건축면적, 건폐율, 연면적,
                #            용적률산정연면적, 용적률, 총동연면적  (단위 ㎡ / %)
                # 구조:      구조, 기타구조
                # 용도:      주용도, 기타용도
                # 지붕:      지붕, 기타지붕
                # 세대:      세대수, 가구수, 호수
                # 층수:      높이(m), 지상층수, 지하층수
                # 승강기:    승용승강기수, 비상용승강기수
                # 부속건축물: 부속건축물수, 부속건축물면적
                # 주차:      옥내/옥외 × 기계식/자주식 주차대수·면적 (8개)
                # 시기:      허가일, 착공일, 사용승인일,
                #            허가번호년, 허가번호기관, 허가번호구분
                # ESG:       에너지효율등급, 에너지절감율, 에너지효율점수,
                #            친환경건축물등급, 친환경건축물인증점수,
                #            지능형건축물등급, 지능형건축물인증점수
                # 내진:      내진설계적용여부, 내진능력
                # 기타:      생성일자
            },

            "층별개요": {
                # 층 이름을 키로 하는 dict. 지하 깊은 층 → 지상 꼭대기
                # 층 순으로 정렬돼 있다. 데이터가 없으면 빈 dict.
                #   키: "1층", "2층" (지상) / "지하1층", "지하2층" (지하)
                #   값: 그 층 항목 dict들의 리스트. 한 층에 용도가 여러 개면
                #       항목이 여러 건이라 리스트로 담는다.
                # 각 항목 dict의 필드 (주소·건물명 등 표제부와 중복되는
                # 필드는 제외, 층마다 다른 정보만):
                #       지상지하구분, 층번호, 층번호표기, 구조, 기타구조,
                #       주용도, 기타용도, 주부속구분, 면적, 면적제외여부
            },

            "지역지구구역": [
                # 용도지역/지구/구역마다 dict 하나. 데이터 없으면 빈 리스트.
                # 필드 (주소·건물명 등 표제부와 중복되는 필드는 제외):
                #       구분, 명칭, 기타지역지구구역, 대표여부(bool)
            ],
        }

    주의:
        - "표제부"는 항상 dict (건물 못 찾으면 값이 전부 None).
        - "층별개요"는 항상 dict (층 이름이 키. 데이터 없으면 빈 dict).
        - "지역지구구역"은 항상 리스트 (데이터 없으면 빈 리스트).
        - 건축HUB는 빈 값을 공백 문자열(" ")로 주는 경우가 많다.
          이 클래스는 공백을 None으로 변환한다.
        - 각 필드의 정확한 영문 원본 키는 아래 _TITLE_MAP / _FLOOR_MAP /
          _ZONE_MAP 매핑 표를 참고.
    """

    # 건축HUB 엔드포인트 — 대장별로 다르다.
    ENDPOINT_TITLE = (
        "https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo"
    )
    ENDPOINT_FLOOR = (
        "https://apis.data.go.kr/1613000/BldRgstHubService/getBrFlrOulnInfo"
    )
    ENDPOINT_ZONE = (
        "https://apis.data.go.kr/1613000/BldRgstHubService/getBrJijiguInfo"
    )

    # ── 표제부: API 영문 키 → 한글 키 (전체 필드) ──────────────────
    _TITLE_MAP = {
        # 식별·주소
        "platPlc": "지번주소",
        "newPlatPlc": "도로명주소",
        "platGbCd": "대지구분코드",
        "mgmBldrgstPk": "관리건축물대장PK",
        "regstrGbCdNm": "대장구분",
        "regstrKindCdNm": "대장종류",
        "bldNm": "건물명",
        "splotNm": "특수지명",
        "block": "블록",
        "lot": "로트",
        "bylotCnt": "외필지수",
        "dongNm": "동명칭",
        "mainAtchGbCdNm": "주부속구분",
        # 규모
        "platArea": "대지면적",
        "archArea": "건축면적",
        "bcRat": "건폐율",
        "totArea": "연면적",
        "vlRatEstmTotArea": "용적률산정연면적",
        "vlRat": "용적률",
        "totDongTotArea": "총동연면적",
        # 구조
        "strctCdNm": "구조",
        "etcStrct": "기타구조",
        # 용도
        "mainPurpsCdNm": "주용도",
        "etcPurps": "기타용도",
        # 지붕
        "roofCdNm": "지붕",
        "etcRoof": "기타지붕",
        # 세대·가구·호
        "hhldCnt": "세대수",
        "fmlyCnt": "가구수",
        "hoCnt": "호수",
        # 층수·높이
        "heit": "높이",
        "grndFlrCnt": "지상층수",
        "ugrndFlrCnt": "지하층수",
        # 승강기
        "rideUseElvtCnt": "승용승강기수",
        "emgenUseElvtCnt": "비상용승강기수",
        # 부속건축물
        "atchBldCnt": "부속건축물수",
        "atchBldArea": "부속건축물면적",
        # 주차 — 옥내기계식
        "indrMechUtcnt": "옥내기계식주차대수",
        "indrMechArea": "옥내기계식주차면적",
        # 주차 — 옥외기계식
        "oudrMechUtcnt": "옥외기계식주차대수",
        "oudrMechArea": "옥외기계식주차면적",
        # 주차 — 옥내자주식
        "indrAutoUtcnt": "옥내자주식주차대수",
        "indrAutoArea": "옥내자주식주차면적",
        # 주차 — 옥외자주식
        "oudrAutoUtcnt": "옥외자주식주차대수",
        "oudrAutoArea": "옥외자주식주차면적",
        # 시기
        "pmsDay": "허가일",
        "stcnsDay": "착공일",
        "useAprDay": "사용승인일",
        "pmsnoYear": "허가번호년",
        "pmsnoKikCdNm": "허가번호기관",
        "pmsnoGbCdNm": "허가번호구분",
        # ESG
        "engrGrade": "에너지효율등급",
        "engrRat": "에너지절감율",
        "engrEpi": "에너지효율점수",
        "gnBldGrade": "친환경건축물등급",
        "gnBldCert": "친환경건축물인증점수",
        "itgBldGrade": "지능형건축물등급",
        "itgBldCert": "지능형건축물인증점수",
        # 내진
        "rserthqkDsgnApplyYn": "내진설계적용여부",
        "rserthqkAblty": "내진능력",
        # 기타
        "crtnDay": "생성일자",
    }
    _TITLE_INT = {
        "외필지수",
        "세대수", "가구수", "호수",
        "지상층수", "지하층수",
        "승용승강기수", "비상용승강기수",
        "부속건축물수",
        "옥내기계식주차대수", "옥외기계식주차대수",
        "옥내자주식주차대수", "옥외자주식주차대수",
    }
    _TITLE_STR = {
        "지번주소", "도로명주소", "대지구분코드",
        "관리건축물대장PK", "대장구분", "대장종류",
        "건물명", "특수지명", "블록", "로트", "동명칭", "주부속구분",
        "구조", "기타구조", "주용도", "기타용도", "지붕", "기타지붕",
        "허가일", "착공일", "사용승인일",
        "허가번호년", "허가번호기관", "허가번호구분",
        "에너지효율등급", "친환경건축물등급", "지능형건축물등급",
        "내진설계적용여부", "내진능력", "생성일자",
    }

    # ── 층별개요: API 영문 키 → 한글 키 ────────────────────────────
    # 주소·건물명·코드 등 표제부에 이미 있는 필드는 빼고, 그 층마다
    # 실제로 다른 정보만 담는다.
    _FLOOR_MAP = {
        "flrGbCdNm": "지상지하구분",
        "flrNo": "층번호",
        "flrNoNm": "층번호표기",
        "strctCdNm": "구조",
        "etcStrct": "기타구조",
        "mainPurpsCdNm": "주용도",
        "etcPurps": "기타용도",
        "mainAtchGbCdNm": "주부속구분",
        "area": "면적",
        "areaExctYn": "면적제외여부",
    }
    _FLOOR_INT = {"층번호"}
    _FLOOR_STR = {
        "지상지하구분", "층번호표기", "구조", "기타구조",
        "주용도", "기타용도", "주부속구분", "면적제외여부",
    }

    # ── 지역지구구역: API 영문 키 → 한글 키 ────────────────────────
    # 주소·건물명·코드 등 표제부에 이미 있는 필드는 빼고, 구역 정보만 담는다.
    _ZONE_MAP = {
        "jijiguGbCdNm": "구분",
        "jijiguCdNm": "명칭",
        "etcJijigu": "기타지역지구구역",
    }
    _ZONE_STR = {
        "구분", "명칭", "기타지역지구구역",
    }

    def __init__(self, service_key):
        """데이터 소스 인스턴스를 만든다.

        Args:
            service_key: 공공데이터포털 건축HUB 건축물대장정보 서비스의
                         일반 인증키(Decoding 키).
        """
        self._service_key = service_key

    def fetch(self, location: LocationInfo) -> dict:
        """LocationInfo를 받아 건축물대장 정보를 dict로 반환한다.

        표제부·층별개요·지역지구구역 세 대장을 모두 호출해 한 dict에 묶는다.

        Args:
            location: 정규화된 위치 정보. bcode, bun, ji를 사용한다.

        Returns:
            {"title": dict, "floors": list, "zones": list} 형태의 dict.
            (클래스 docstring의 스키마 참고)

        Raises:
            ValueError:   location에 bcode 또는 bun이 없는 경우.
            RuntimeError: API 응답이 비정상인 경우.
        """
        # 1) 호출 키 준비.
        if location.bcode is None or location.bun is None:
            raise ValueError(
                "BuildingRegisterRepository는 bcode와 bun이 필요합니다."
            )

        sigungu_code = location.bcode[:5]
        bdong_code = location.bcode[5:]
        # 건축HUB는 본번/부번을 4자리 0-패딩 형식으로 받는다.
        bun = str(location.bun).zfill(4)
        ji = str(location.ji).zfill(4) if location.ji is not None else "0000"

        # 2) 세 대장을 각각 호출한다.
        title_items = self._call_api(
            self.ENDPOINT_TITLE, sigungu_code, bdong_code, bun, ji
        )
        floor_items = self._call_api(
            self.ENDPOINT_FLOOR, sigungu_code, bdong_code, bun, ji
        )
        zone_items = self._call_api(
            self.ENDPOINT_ZONE, sigungu_code, bdong_code, bun, ji
        )

        # 3) 각 대장 결과를 우리 스키마(한글 키)로 변환.
        return {
            "표제부": self._build_title(title_items),
            "층별개요": self._build_floors(floor_items),
            "지역지구구역": self._build_zones(zone_items),
        }

    # ── API 호출 ──────────────────────────────────────────────────

    def _call_api(self, endpoint, sigungu_code, bdong_code, bun, ji):
        """건축HUB 한 대장 API를 호출하고 item 리스트를 반환한다 (내부 메서드).

        세 대장 모두 요청 파라미터가 같아 이 메서드를 공유한다. 엔드포인트만
        인자로 받는다.

        Args:
            endpoint: 호출할 대장의 엔드포인트 URL.
            sigungu_code, bdong_code, bun, ji: 조회 키.

        Returns:
            응답의 item 리스트. 결과가 없으면 빈 리스트.
            (item이 1건이면 API가 dict로 주지만, 항상 리스트로 통일해 반환)

        Raises:
            RuntimeError: HTTP 오류, JSON 파싱 실패, API 비정상 응답인 경우.
        """
        params = {
            "serviceKey": self._service_key,
            "sigunguCd": sigungu_code,
            "bjdongCd": bdong_code,
            "bun": bun,
            "ji": ji,
            "numOfRows": "100",   # 층이 많은 건물도 한 번에 받도록 넉넉히
            "pageNo": "1",
            "_type": "json",
        }

        # 호출.
        try:
            response = requests.get(endpoint, params=params, timeout=10)
            response_body = response.text
        except Exception as e:
            raise RuntimeError("건축HUB API 호출 실패: " + str(e))

        # JSON 파싱. 실패하면 XML 에러 응답일 가능성이 높다.
        try:
            data = json.loads(response_body)
        except json.JSONDecodeError:
            raise RuntimeError(
                "건축HUB API가 JSON이 아닌 응답을 반환했습니다. "
                "(키 오류 또는 서버 오류 가능) 응답 원문: "
                + response_body[:500]
            )

        # 헤더 결과 코드 확인.
        header = data.get("response", {}).get("header", {})
        result_code = header.get("resultCode")
        if result_code != "00":
            result_msg = header.get("resultMsg", "알 수 없는 오류")
            raise RuntimeError(
                "건축HUB API 오류: " + str(result_code) + " " + str(result_msg)
            )

        # body에서 item 추출.
        body = data.get("response", {}).get("body", {})
        if str(body.get("totalCount", 0)) == "0":
            return []

        items = body.get("items", {})
        if not items:
            return []
        item = items.get("item")
        if item is None:
            return []

        # item은 1건이면 dict, 여러 건이면 list로 온다. 리스트로 통일.
        if isinstance(item, list):
            return item
        return [item]

    # ── 표제부 변환 ───────────────────────────────────────────────

    def _build_title(self, items) -> dict:
        """표제부 item 리스트를 단일 dict로 변환한다 (내부 메서드).

        한 필지에 건물이 여러 동이면 여러 건이 오지만, 단독 상가건물을
        가정하여 첫 건만 사용한다. 결과가 없으면 모든 값이 None인 dict.
        """
        if not items:
            return {k: None for k in self._TITLE_MAP.values()}
        return self._convert(
            items[0], self._TITLE_MAP, self._TITLE_INT, self._TITLE_STR
        )

    # ── 층별개요 변환 ─────────────────────────────────────────────

    def _build_floors(self, items) -> dict:
        """층별개요 item 리스트를 '층별 dict'로 변환한다 (내부 메서드).

        반환 형태:
            {
                "지하3층": [ {그 층의 항목}, ... ],
                "지하1층": [ ... ],
                "1층":     [ {음식점 항목}, {부동산중개 항목} ],
                "2층":     [ ... ],
                ...
            }
            - 키: 층 이름. 지상은 "1층"·"2층", 지하는 "지하1층"·"지하2층".
            - 값: 그 층에 속한 항목 dict들의 리스트.
              건축HUB는 한 층에 용도가 여러 개면 항목을 여러 건 주므로
              (예: 2층 음식점 + 2층 계단실), 한 층이라도 리스트로 담는다.
            - 키 순서: 지하 깊은 층 → 지상 꼭대기 층 순으로 정렬.
            - 결과가 없으면 빈 dict.

        Args:
            items: 층별개요 API item 리스트.

        Returns:
            층 이름을 키로 하는 dict.
        """
        # 1) 각 item을 우리 스키마로 변환하면서, 정렬용 순서값과 함께 모은다.
        #    순서값: 지하 N층 = -N, 지상 N층 = +N.
        #    이러면 지하5층(-5) < 지하1층(-1) < 1층(+1) < 15층(+15) 순이 된다.
        converted = []
        for item in items:
            floor = self._convert(
                item, self._FLOOR_MAP, self._FLOOR_INT, self._FLOOR_STR
            )

            gb = floor.get("지상지하구분")        # "지상" / "지하"
            num = floor.get("층번호")             # 정수 (예: 3)

            if num is None:
                # 층번호가 없으면 정렬·키 생성이 불가. "기타"로 모은다.
                key = "기타"
                order = 9999
            elif gb == "지하":
                key = "지하" + str(num) + "층"
                order = -num
            else:
                # 지상(또는 구분 불명)은 지상으로 취급.
                key = str(num) + "층"
                order = num

            converted.append((order, key, floor))

        # 2) 순서값 기준으로 정렬 (지하 깊은 층 → 지상 꼭대기 층).
        converted.sort(key=lambda x: x[0])

        # 3) 층 이름을 키로 하는 dict에 담는다.
        #    한 층에 항목이 여러 개일 수 있으므로 값은 리스트.
        floors = {}
        for order, key, floor in converted:
            if key not in floors:
                floors[key] = []
            floors[key].append(floor)
        return floors

    # ── 지역지구구역 변환 ─────────────────────────────────────────

    def _build_zones(self, items) -> list:
        """지역지구구역 item 리스트를 dict 리스트로 변환한다 (내부 메서드).

        용도지역/지구/구역마다 dict 하나. 결과가 없으면 빈 리스트.
        reprYn("1"/"0")은 불리언으로 변환한다.
        """
        zones = []
        for item in items:
            zone = self._convert(
                item, self._ZONE_MAP, set(), self._ZONE_STR
            )
            # reprYn은 "1"이면 대표 용도지역. 불리언으로 바꾼다.
            raw_repr = item.get("reprYn")
            zone["대표여부"] = (str(raw_repr).strip() == "1")
            zones.append(zone)
        return zones

    # ── 공통 변환 헬퍼 ────────────────────────────────────────────

    def _convert(self, item, field_map, int_keys, str_keys) -> dict:
        """API item(dict)을 우리 스키마 dict로 변환한다 (내부 메서드).

        세 대장이 변환 방식이 같아 이 메서드를 공유한다. 매핑 표와
        타입 분류만 인자로 다르게 받는다.

        Args:
            item:      API 응답 항목 하나 (dict).
            field_map: {API 영문 키: 우리 키} 매핑.
            int_keys:  정수로 변환할 우리 키 집합.
            str_keys:  문자열로 둘 우리 키 집합. 나머지는 float.

        Returns:
            우리 키로 된 dict. 빈 값은 None.
        """
        result = {}
        for api_key, our_key in field_map.items():
            if api_key not in item:
                result[our_key] = None
                continue

            value = item[api_key]
            if self._is_empty(value):
                result[our_key] = None
                continue

            if our_key in str_keys:
                result[our_key] = str(value).strip()
            elif our_key in int_keys:
                result[our_key] = self._to_int(value)
            else:
                result[our_key] = self._to_float(value)
        return result

    def _is_empty(self, value) -> bool:
        """값이 비어있는지(None, 공백 문자열) 판단한다 (내부 메서드).

        건축HUB는 빈 값을 공백(" ")으로 주는 경우가 많아 이 처리가 필요하다.
        """
        if value is None:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True
        return False

    def _to_int(self, value):
        """값을 정수로 변환한다. 실패하면 None (내부 메서드)."""
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None

    def _to_float(self, value):
        """값을 실수로 변환한다. 실패하면 None (내부 메서드)."""
        try:
            return float(value)
        except (ValueError, TypeError):
            return None