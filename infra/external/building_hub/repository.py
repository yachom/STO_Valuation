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
    HTTP envelope/인증/JSON 파싱은 BuildingHubClient가 담당한다. 본 모듈은
    엔드포인트 지정 + API 영문 키 → 한글 키 매핑 + 타입 변환만 책임진다.

API 키:
    공공데이터포털 '건축HUB_건축물대장정보 서비스'의 일반 인증키(Decoding).
    BuildingHubClient 생성자에서 받는다.
"""

from infra.external.building_hub.client import BuildingHubClient
from real_estate.location import LocationInfo


class BuildingRegisterRepository:
    """건축HUB 건축물대장 조회 데이터 소스.

    사용법:
        client = BuildingHubClient(service_key="공공데이터포털_Decoding_키")
        repo = BuildingRegisterRepository(client=client)
        facts = repo.fetch(location)

    fetch() 반환 dict 스키마 (키는 모두 한글):
        {
            "표제부":      dict,   # 건물 전체 정보. 못 찾으면 값이 전부 None.
            "층별개요":    dict,   # 층 이름 → [그 층 항목 dict, ...]. 없으면 빈 dict.
            "지역지구구역": list,   # 용도지역/지구/구역 dict의 리스트. 없으면 빈 list.
        }

    주의:
        - 건축HUB는 빈 값을 공백(" ")으로 주는 경우가 많아 None으로 변환한다.
        - 각 필드의 영문 원본 키 ↔ 한글 키 매핑은 _TITLE_MAP / _FLOOR_MAP /
          _ZONE_MAP 표를 참고.
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
    _ZONE_MAP = {
        "jijiguGbCdNm": "구분",
        "jijiguCdNm": "명칭",
        "etcJijigu": "기타지역지구구역",
    }
    _ZONE_STR = {
        "구분", "명칭", "기타지역지구구역",
    }

    def __init__(self, client: BuildingHubClient):
        self._client = client

    def fetch(self, location: LocationInfo) -> dict:
        """LocationInfo를 받아 건축물대장 정보를 dict로 반환한다.

        Raises:
            ValueError:        location에 bcode 또는 bun이 없는 경우.
            BuildingHubError:  API 호출/응답이 비정상인 경우.
        """
        if location.bcode is None or location.bun is None:
            raise ValueError(
                "BuildingRegisterRepository는 bcode와 bun이 필요합니다."
            )

        sigungu_code = location.bcode[:5]
        bdong_code = location.bcode[5:]
        # 건축HUB는 본번/부번을 4자리 0-패딩 형식으로 받는다.
        bun = str(location.bun).zfill(4)
        ji = str(location.ji).zfill(4) if location.ji is not None else "0000"

        title_items = self._client.get_items(
            self.ENDPOINT_TITLE, sigungu_code, bdong_code, bun, ji
        )
        floor_items = self._client.get_items(
            self.ENDPOINT_FLOOR, sigungu_code, bdong_code, bun, ji
        )
        zone_items = self._client.get_items(
            self.ENDPOINT_ZONE, sigungu_code, bdong_code, bun, ji
        )

        return {
            "표제부": self._build_title(title_items),
            "층별개요": self._build_floors(floor_items),
            "지역지구구역": self._build_zones(zone_items),
        }

    # ── 표제부 변환 ───────────────────────────────────────────────

    def _build_title(self, items) -> dict:
        if not items:
            return {k: None for k in self._TITLE_MAP.values()}
        return self._convert(
            items[0], self._TITLE_MAP, self._TITLE_INT, self._TITLE_STR
        )

    # ── 층별개요 변환 ─────────────────────────────────────────────

    def _build_floors(self, items) -> dict:
        """층별개요 item 리스트를 '층별 dict'로 변환한다.

        키는 "지하3층"·"1층" 같은 층 이름, 값은 그 층 항목 dict들의 리스트.
        지하 깊은 층 → 지상 꼭대기 층 순으로 정렬. 결과 없으면 빈 dict.
        """
        converted = []
        for item in items:
            floor = self._convert(
                item, self._FLOOR_MAP, self._FLOOR_INT, self._FLOOR_STR
            )

            gb = floor.get("지상지하구분")
            num = floor.get("층번호")

            if num is None:
                key = "기타"
                order = 9999
            elif gb == "지하":
                key = "지하" + str(num) + "층"
                order = -num
            else:
                key = str(num) + "층"
                order = num

            converted.append((order, key, floor))

        converted.sort(key=lambda x: x[0])

        floors: dict = {}
        for _order, key, floor in converted:
            floors.setdefault(key, []).append(floor)
        return floors

    # ── 지역지구구역 변환 ─────────────────────────────────────────

    def _build_zones(self, items) -> list:
        zones = []
        for item in items:
            zone = self._convert(
                item, self._ZONE_MAP, set(), self._ZONE_STR
            )
            raw_repr = item.get("reprYn")
            zone["대표여부"] = (str(raw_repr).strip() == "1")
            zones.append(zone)
        return zones

    # ── 공통 변환 헬퍼 ────────────────────────────────────────────

    def _convert(self, item, field_map, int_keys, str_keys) -> dict:
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
        if value is None:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True
        return False

    def _to_int(self, value):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None

    def _to_float(self, value):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
