"""위치 정보 모델과 주소 정규화.

이 파일은 두 가지 클래스를 담는다.

    1. LocationInfo
       - 주소 정규화 결과를 담는 데이터 객체.
       - 필드만 갖고 동작은 없다 (값 객체).

    2. LocationResolver
       - 자유 형식 주소 문자열을 입력으로 받아 외부 API들을 호출하여
         LocationInfo 객체를 만들어 반환한다.
       - 호출하는 API:
            (a) 카카오 주소 검색  → 정규화된 도로명/지번 주소 + 좌표
            (b) 카카오 좌표→행정구역 → 시도·시군구·행정동·법정동 정보
            (c) Juso 도로명주소 API → 건물관리번호 + 본번/부번

설계 의도:
    - 데이터를 담는 책임(LocationInfo)과 만드는 책임(LocationResolver)을 분리.
    - 호출하는 쪽 코드는 LocationResolver의 resolve() 메서드 하나만 알면 된다.
    - API 호출 세부사항(URL, 헤더, 응답 파싱)은 LocationResolver 내부에
      캡슐화되어 외부로 새지 않는다.
    - 다운스트림 데이터 소스들이 자기에게 필요한 필드(주소/좌표/행정동/법정동/
      구/건물관리번호/본번·부번 등)를 LocationInfo에서 골라 쓰도록 풍부한
      필드를 제공한다.
"""

import os
import urllib.parse
import urllib.request
import json


def core_road_address(road: str) -> str:
    """전체 도로명주소에서 '도로명 + 건물번호'만 남긴다.

    '서울 강서구 공항대로 209' → '공항대로 209'.

    시/도·시군구 접두는 데이터 소스마다 표기가 달라(예: 해소값 '서울' vs
    LocalData 저장값 '서울특별시') 부분일치·LIKE 매칭을 깨뜨린다. 도로명
    토큰(…로/…길)부터 끝까지만 남겨 그 불일치를 제거한다. LocalData 인허가
    조회, navigator 검색어 고유명사 매칭 등 도로명 기반 매칭의 공용 정규화.
    """
    toks = (road or "").split()
    for i, t in enumerate(toks):
        if t.endswith("로") or t.endswith("길"):
            return " ".join(toks[i:])
    return road or ""


class LocationInfo:
    """주소 정규화 결과를 담는 값 객체.

    한 번 만들어지면 필드를 바꾸지 않고 그대로 사용한다 (관례).
    다른 컴포넌트들이 이 객체를 받아 자신에게 필요한 필드만 골라 쓴다.

    필드:
        raw_address:     사용자가 입력한 원본 주소 문자열
        road_address:    카카오가 반환한 정규화된 도로명 주소
                         (도로명 정보가 없는 주소면 None)
        jibun_address:   카카오가 반환한 지번 주소
                         (지번 정보가 없는 주소면 None)
        latitude:        위도 (실수)
        longitude:       경도 (실수)
        sido:            시도 이름 (예: "서울특별시")
        sigungu:         시군구 이름 (예: "강북구")
        hcode:           행정동 코드 (10자리 문자열)
        hname:           행정동 이름 (예: "미아1동")
        bcode:           법정동 코드 (10자리 문자열)
        bname:           법정동 이름 (예: "미아동")
        bd_mgt_sn:       건물관리번호 (25자리 문자열, 건물 단위 영구 유일 ID)
                         Juso API 조회 실패 시 None
        bun:             지번 본번 (문자열, 예: "21")
                         Juso API 조회 실패 시 None
        ji:              지번 부번 (문자열, 예: "15". 부번이 없으면 "0")
                         Juso API 조회 실패 시 None

    행정동과 법정동:
        - 행정동(H): 행정 편의를 위한 구분. 숫자 접미사가 붙음(미아1동, 미아2동).
                     인구·상권 등 행정 데이터에서 주로 사용.
        - 법정동(B): 법적 주소 단위. 보통 숫자가 없음(미아동).
                     부동산·토지·건축 데이터에서 주로 사용.
        팀원은 자기 DB의 키 형식에 맞춰 hname 또는 bname을 선택해 사용한다.

    건물관리번호(bd_mgt_sn):
        - 한 건물 = 한 번호 (1:1, 영구 고정).
        - 건물 단위 데이터를 매칭할 때 가장 신뢰할 수 있는 키.
        - Juso API 조회 실패 시 None이므로 사용 전 None 체크.

    본번/부번(bun, ji):
        - 건축HUB 건축물대장 API 호출에 필요하다 (시군구코드+법정동코드+본번+부번).
        - bcode(법정동코드 10자리)와 함께 쓴다.
          bcode 앞 5자리 = 시군구코드, 뒤 5자리 = 법정동코드.
        - Juso API 조회 실패 시 None.
    """

    def __init__(
        self,
        raw_address,
        road_address,
        jibun_address,
        latitude,
        longitude,
        sido,
        sigungu,
        hcode,
        hname,
        bcode,
        bname,
        bd_mgt_sn,
        bun,
        ji,
    ):
        """객체 생성 시 모든 필드를 한 번에 받아 저장한다.

        Args:
            raw_address:   원본 주소 문자열
            road_address:  정규화된 도로명 주소 (또는 None)
            jibun_address: 지번 주소 (또는 None)
            latitude:      위도
            longitude:     경도
            sido:          시도 이름
            sigungu:       시군구 이름
            hcode:         행정동 코드
            hname:         행정동 이름
            bcode:         법정동 코드
            bname:         법정동 이름
            bd_mgt_sn:     건물관리번호 (25자리) 또는 None
            bun:           지번 본번 또는 None
            ji:            지번 부번 또는 None
        """
        self.raw_address = raw_address
        self.road_address = road_address
        self.jibun_address = jibun_address
        self.latitude = latitude
        self.longitude = longitude
        self.sido = sido
        self.sigungu = sigungu
        self.hcode = hcode
        self.hname = hname
        self.bcode = bcode
        self.bname = bname
        self.bd_mgt_sn = bd_mgt_sn
        self.bun = bun
        self.ji = ji

    def __repr__(self):
        """객체를 출력했을 때 사람이 읽기 쉬운 형태로 보여준다.

        print(location) 했을 때 어떤 값들이 들어있는지 확인할 수 있도록
        이 메서드를 정의한다. 디버깅에 유용하다.
        """
        return (
            "LocationInfo("
            "raw_address=" + repr(self.raw_address) + ", "
            "road_address=" + repr(self.road_address) + ", "
            "jibun_address=" + repr(self.jibun_address) + ", "
            "latitude=" + str(self.latitude) + ", "
            "longitude=" + str(self.longitude) + ", "
            "sido=" + repr(self.sido) + ", "
            "sigungu=" + repr(self.sigungu) + ", "
            "hcode=" + repr(self.hcode) + ", "
            "hname=" + repr(self.hname) + ", "
            "bcode=" + repr(self.bcode) + ", "
            "bname=" + repr(self.bname) + ", "
            "bd_mgt_sn=" + repr(self.bd_mgt_sn) + ", "
            "bun=" + repr(self.bun) + ", "
            "ji=" + repr(self.ji) + ")"
        )


class LocationResolver:
    """자유 형식 주소를 외부 API로 정규화하여 LocationInfo로 반환한다.

    사용법:
        resolver = LocationResolver(
            kakao_api_key="...",
            juso_api_key="...",
        )
        location = resolver.resolve("서울 강남구 테헤란로 123")

    내부 동작:
        1. 카카오 주소 검색 API
            정규화된 도로명/지번 주소와 좌표를 얻는다.

        2. 카카오 좌표→행정구역 API
            좌표로 시도·시군구·행정동·법정동 정보를 얻는다.

        3. Juso 도로명주소 API
            건물관리번호(bdMgtSn)와 지번 본번/부번을 얻는다.
            지번 주소로 먼저 시도하고, 실패하면 도로명 주소로 재시도한다.
            둘 다 실패하면 bd_mgt_sn·bun·ji는 None으로 둔다 (예외 안 던짐).
    """

    # 클래스 상수: API 엔드포인트 URL.
    # self 없이 클래스 자체에 묶여 있어, 모든 인스턴스가 공유한다.
    ADDRESS_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/address.json"
    COORD_TO_REGION_URL = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"
    JUSO_ADDR_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

    def __init__(self, kakao_api_key, juso_api_key):
        """리졸버 인스턴스를 만든다.

        Args:
            kakao_api_key: 카카오 REST API 키.
            juso_api_key:  Juso(도로명주소) API 승인키.
        """
        self.kakao_api_key = kakao_api_key
        self.juso_api_key = juso_api_key

    def resolve(self, raw_address):
        """주소 문자열을 받아 정규화된 LocationInfo를 반환한다.

        Args:
            raw_address: 사용자가 입력한 주소 문자열.
                         예: "서울 강남구 테헤란로 123" 또는 "강남구 역삼동 825".

        Returns:
            LocationInfo 객체.
            카카오 관련 필드는 항상 채워진다.
            bd_mgt_sn·bun·ji는 Juso 조회 실패 시 None일 수 있다.

        Raises:
            LookupError: 주소를 카카오에서 찾지 못한 경우.
            RuntimeError: 카카오 API 호출 자체가 실패한 경우 (네트워크 오류 등).
        """
        # 1단계: 카카오 주소 검색 — 좌표 + 정규화된 주소(도로명+지번).
        road_address, jibun_address, latitude, longitude = self._search_address(
            raw_address
        )

        # 2단계: 카카오 좌표→행정구역 — 시도·시군구·행정동·법정동.
        sido, sigungu, hcode, hname, bcode, bname = self._coord_to_region(
            latitude, longitude
        )

        # 3단계: Juso API — 건물관리번호 + 본번/부번.
        #   지번 주소로 먼저 시도하고, 실패하면 도로명 주소로 재시도.
        bd_mgt_sn, bun, ji = self._resolve_juso(jibun_address, road_address)

        # 4단계: 모은 정보로 LocationInfo 객체를 만들어 반환한다.
        return LocationInfo(
            raw_address=raw_address,
            road_address=road_address,
            jibun_address=jibun_address,
            latitude=latitude,
            longitude=longitude,
            sido=sido,
            sigungu=sigungu,
            hcode=hcode,
            hname=hname,
            bcode=bcode,
            bname=bname,
            bd_mgt_sn=bd_mgt_sn,
            bun=bun,
            ji=ji,
        )

    def _search_address(self, raw_address):
        """카카오 주소 검색 API를 호출한다 (내부 메서드).

        함수 이름이 _로 시작하는 것은 "이 메서드는 외부에서 직접 부르지 말고
        같은 클래스 안에서만 쓴다"는 파이썬의 관례적 표시다.

        Args:
            raw_address: 원본 주소 문자열.

        Returns:
            (도로명 주소, 지번 주소, 위도, 경도) 네 값을 튜플로 묶어 반환.
            도로명 또는 지번이 응답에 없으면 그 자리에 None이 들어간다.
        """
        # 쿼리 문자열을 URL에 안전하게 인코딩 (한글, 공백 처리)
        query = urllib.parse.urlencode({"query": raw_address})
        url = self.ADDRESS_SEARCH_URL + "?" + query

        # HTTP 요청에 카카오 API 키를 헤더에 실어 보낸다.
        request = urllib.request.Request(url)
        request.add_header("Authorization", "KakaoAK " + self.kakao_api_key)

        # 실제 호출. 응답을 받아 JSON으로 파싱.
        response = urllib.request.urlopen(request)
        response_body = response.read().decode("utf-8")
        data = json.loads(response_body)

        # 카카오는 결과를 documents 리스트로 준다. 없으면 주소를 못 찾은 것.
        documents = data["documents"]
        if len(documents) == 0:
            raise LookupError("주소를 찾을 수 없습니다: " + raw_address)

        # 첫 번째 결과만 사용 (가장 매칭도가 높은 후보).
        first = documents[0]

        # 도로명 주소: road_address 키가 None인 경우(도로명 없는 주소)는 None 저장.
        if first["road_address"] is not None:
            road_address = first["road_address"]["address_name"]
        else:
            road_address = None

        # 지번 주소: address 키가 None인 경우는 None 저장.
        # 보통 address는 거의 항상 있고, road_address가 없을 수 있다.
        if first["address"] is not None:
            jibun_address = first["address"]["address_name"]
        else:
            jibun_address = None

        # 카카오는 좌표를 문자열로 준다. 실수로 변환.
        # x=경도, y=위도 라는 점에 주의 (지리 좌표 관례와 반대).
        latitude = float(first["y"])
        longitude = float(first["x"])

        return road_address, jibun_address, latitude, longitude

    def _coord_to_region(self, latitude, longitude):
        """좌표를 시도·시군구·행정동·법정동 정보로 변환한다 (내부 메서드).

        카카오 응답에는 같은 좌표에 대해 행정동(H)과 법정동(B) 두 항목이
        함께 들어온다. 한 번의 호출로 둘 다 빼낼 수 있다. 시도·시군구는
        둘 어느 쪽에서 빼도 동일하다.

        Args:
            latitude:  위도
            longitude: 경도

        Returns:
            (시도, 시군구, 행정동코드, 행정동이름, 법정동코드, 법정동이름) 튜플.
            예: ("서울특별시", "강북구", "1130561000", "미아1동",
                 "1130510700", "미아동")

        Raises:
            LookupError: 행정동 또는 법정동 정보가 응답에 없는 경우.
        """
        # 카카오는 x=경도, y=위도 순서.
        query = urllib.parse.urlencode({"x": longitude, "y": latitude})
        url = self.COORD_TO_REGION_URL + "?" + query

        request = urllib.request.Request(url)
        request.add_header("Authorization", "KakaoAK " + self.kakao_api_key)

        response = urllib.request.urlopen(request)
        response_body = response.read().decode("utf-8")
        data = json.loads(response_body)

        # 응답에는 행정동(H)과 법정동(B) 두 종류가 들어있다. 둘 다 빼낸다.
        # region_1depth_name = 시도, region_2depth_name = 시군구,
        # region_3depth_name = 동(행정동 또는 법정동)
        documents = data["documents"]

        hcode = None
        hname = None
        bcode = None
        bname = None
        sido = None
        sigungu = None

        for doc in documents:
            if doc["region_type"] == "H":
                hcode = doc["code"]
                hname = doc["region_3depth_name"]
                # 시도·시군구는 H에서 한 번만 채우면 충분.
                sido = doc["region_1depth_name"]
                sigungu = doc["region_2depth_name"]
            elif doc["region_type"] == "B":
                bcode = doc["code"]
                bname = doc["region_3depth_name"]
                # 시도·시군구가 H에 없었을 경우를 대비해 B에서도 채워둔다.
                if sido is None:
                    sido = doc["region_1depth_name"]
                if sigungu is None:
                    sigungu = doc["region_2depth_name"]

        if hcode is None or bcode is None:
            raise LookupError(
                "좌표에 해당하는 행정동/법정동 정보를 찾을 수 없습니다: "
                + str(latitude) + ", " + str(longitude)
            )

        return sido, sigungu, hcode, hname, bcode, bname

    def _resolve_juso(self, jibun_address, road_address):
        """건물관리번호와 본번/부번을 조회한다 (내부 메서드).

        지번 주소로 먼저 Juso API를 시도하고, 결과가 없으면 도로명 주소로
        재시도한다. 둘 다 실패하면 (None, None, None)을 반환한다
        (예외를 던지지 않는다).

        Juso 정보(건물관리번호·본번·부번)는 없어도 시스템의 다른 부분이
        동작할 수 있으므로, 조회 실패를 치명적 에러로 취급하지 않는다.
        다운스트림에서 None 체크로 처리하면 된다.

        Args:
            jibun_address: 카카오가 정규화한 지번 주소 (또는 None).
            road_address:  카카오가 정규화한 도로명 주소 (또는 None).

        Returns:
            (건물관리번호, 본번, 부번) 튜플. 조회 실패 시 해당 자리는 None.
        """
        # 1순위: 지번 주소로 시도.
        if jibun_address is not None:
            result = self._fetch_juso(jibun_address)
            if result is not None:
                return result

        # 2순위: 지번이 없거나 실패하면 도로명 주소로 재시도.
        if road_address is not None:
            result = self._fetch_juso(road_address)
            if result is not None:
                return result

        # 둘 다 실패. Juso 정보 없이 진행.
        return None, None, None

    def _fetch_juso(self, address):
        """Juso API를 한 번 호출하여 건물관리번호·본번·부번을 얻는다 (내부 메서드).

        검색 결과가 여러 건일 수 있으므로, 입력 주소의 마지막 토큰(번지)을
        응답의 본번/부번과 대조하여 정확히 일치하는 건을 고른다. 일치하는
        것이 없으면 Juso가 가장 정확하다고 판단한 첫 번째 결과를 쓴다.

        Args:
            address: 조회할 주소 문자열 (지번 또는 도로명).

        Returns:
            (건물관리번호, 본번, 부번) 튜플.
            조회 실패(주소 못 찾음, API 오류, 네트워크 오류 등) 시 None.
        """
        # Juso API 요청 파라미터 구성.
        params = urllib.parse.urlencode({
            "confmKey": self.juso_api_key,
            "currentPage": 1,
            "countPerPage": 10,    # 여러 건이 와도 아래에서 필터링한다.
            "keyword": address,
            "resultType": "json",
        })
        url = self.JUSO_ADDR_URL + "?" + params

        # 네트워크 오류·파싱 오류는 모두 "조회 실패(None)"로 처리한다.
        try:
            response = urllib.request.urlopen(url)
            response_body = response.read().decode("utf-8")
            data = json.loads(response_body)
        except Exception:
            return None

        # Juso API의 내부 오류 코드 확인. "0"이 아니면 실패.
        results = data.get("results", {})
        common = results.get("common", {})
        if common.get("errorCode") != "0":
            return None

        # 검색 결과 리스트. 비어있으면 주소를 못 찾은 것.
        juso_list = results.get("juso", [])
        if not juso_list:
            return None

        # 입력 주소의 마지막 단어(번지)를 추출. 예: "...봉천동 21-15" → "21-15"
        tokens = address.strip().split()
        last_token = tokens[-1] if tokens else ""

        # 결과를 돌면서 본번/부번이 입력 번지와 정확히 일치하는 건을 찾는다.
        for juso in juso_list:
            # 도로명 건물번호 조합: 부번이 0이면 "65", 아니면 "65-5".
            buld_main = str(juso.get("buldMnnm", ""))
            buld_sub = str(juso.get("buldSlno", "0"))
            road_num = buld_main if buld_sub == "0" else buld_main + "-" + buld_sub

            # 지번 번호 조합: 같은 방식.
            lnbr_main = str(juso.get("lnbrMnnm", ""))
            lnbr_sub = str(juso.get("lnbrSlno", "0"))
            jibun_num = lnbr_main if lnbr_sub == "0" else lnbr_main + "-" + lnbr_sub

            if last_token == road_num or last_token == jibun_num:
                return self._extract_juso_fields(juso)

        # 정확히 일치하는 번지를 못 찾았으면, Juso가 1순위로 준 결과를 쓴다.
        # (건물명으로 검색했거나 번지 표기가 다른 경우)
        return self._extract_juso_fields(juso_list[0])

    def _extract_juso_fields(self, juso):
        """Juso 결과 한 건에서 (건물관리번호, 본번, 부번)을 뽑는다 (내부 메서드).

        Args:
            juso: Juso API 응답의 juso 리스트 항목 하나 (dict).

        Returns:
            (건물관리번호, 본번, 부번) 튜플. 본번/부번은 지번 기준
            (lnbrMnnm, lnbrSlno).
        """
        bd_mgt_sn = juso.get("bdMgtSn")
        bun = juso.get("lnbrMnnm")     # 지번 본번
        ji = juso.get("lnbrSlno")      # 지번 부번 (없으면 보통 "0")
        return bd_mgt_sn, bun, ji
