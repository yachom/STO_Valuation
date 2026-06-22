"""주소 회수 공통 코어 — AddressEnricher / JibunDeriver / NameLookup.

efficiency_pipeline.py 와 gseed_pipeline.py 가 바이트 단위로 같던 회수 로직을
한 곳에 모은다. 두 파이프라인은 DB 행의 컬럼명만 다르다
(efficiency: rqid/loc_addr/bld_nm, gseed: id/address/building_name).
그래서 DB 행에서 값을 뽑는 부분은 **주입된 키(문자열)** 로 파라미터화한다.

DB 접근 자체는 db 객체의 메서드로 한다 — 두 DB 클래스(EfficiencyDB/GseedDB)가
이미 동일한 이름의 메서드를 노출한다:
    select_code_to_loc()  select_with_bdmgt_no_jibun()  select_missing_bdmgt()
    update_jibun(row_id, jibun)   update_bdmgt(row_id, pk)   commit()

행마다 다른 컬럼명만 주입한다:
    row_id_key  PK 컬럼 ("rqid" / "id")
    loc_key     주소 컬럼 ("loc_addr" / "address")
    name_key    건물명 컬럼 ("bld_nm" / "building_name")

동작은 원본 두 파일과 정확히 같다. 외부 호출 게이트웨이 wait는 호출 측에서
주입한 _GW를 그대로 쓴다 (AddressEnricher 생성 시 받음).
"""

import re
import signal
from collections import Counter

import requests

from real_estate.database.db_builder import patterns


# ============================================================
# 단계 3·4 보조: 카카오 + Juso 호출 (주소 enrich)
# ============================================================
class AddressEnricher:
    """카카오 주소검색으로 jibun_addr, Juso로 bdmgt_sn을 채운다.

    두 API 모두 단건 호출 함수를 제공한다. 호출 실패·결과 없음은 None 반환
    (예외를 던지지 않음). 호출 측이 None을 보고 적절히 처리한다.

    gateway / 엔드포인트 / 타임아웃은 생성 시 주입한다 (파이프라인별 상수 보존).
    """

    def __init__(
        self,
        kakao_key,
        juso_key,
        *,
        gateway,
        kakao_geocode_url,
        kakao_keyword_url,
        juso_url,
        request_timeout,
    ):
        self.kakao_key = kakao_key
        self.juso_key = juso_key
        self._gw = gateway
        self._kakao_geocode_url = kakao_geocode_url
        self._kakao_keyword_url = kakao_keyword_url
        self._juso_url = juso_url
        self._timeout = request_timeout

    # ---------- 카카오 ----------
    def fetch_jibun_addr(self, raw_addr):
        """카카오 주소검색 → 정규화된 지번주소. 실패 시 None."""
        if not raw_addr or not self.kakao_key:
            return None
        try:
            self._gw.wait("kakao_local")
            r = requests.get(
                self._kakao_geocode_url,
                params={"query": raw_addr},
                headers={"Authorization": f"KakaoAK {self.kakao_key}"},
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return None

        docs = data.get("documents") or []
        if not docs:
            return None
        first = docs[0]
        addr = first.get("address") or {}
        return addr.get("address_name") or None

    def search_keyword(self, query):
        """카카오 키워드(장소)검색 → 후보 목록. 실패 시 []."""
        if not query or not self.kakao_key:
            return []
        try:
            self._gw.wait("kakao_local")
            r = requests.get(
                self._kakao_keyword_url,
                params={"query": query, "size": 15},
                headers={"Authorization": f"KakaoAK {self.kakao_key}"},
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return []
        return data.get("documents") or []

    # ---------- Juso ----------
    def fetch_bdmgt_sn(self, keyword):
        """Juso API → 25자리 건물관리번호.

        검색 결과 중 입력 키워드의 마지막 토큰(지번 N 또는 N-N)과 본번/부번이
        정확히 일치하는 결과만 채택. 일치가 없으면 None (첫 결과 무작위 폴백 금지).
        """
        if not keyword or not self.juso_key:
            return None
        try:
            self._gw.wait("juso_addr_link")
            r = requests.get(
                self._juso_url,
                params={
                    "confmKey": self.juso_key,
                    "currentPage": 1,
                    "countPerPage": 10,
                    "keyword": keyword,
                    "resultType": "json",
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return None

        common = data.get("results", {}).get("common", {})
        if common.get("errorCode") != "0":
            return None
        juso_list = data.get("results", {}).get("juso") or []
        if not juso_list:
            return None

        # 마지막 토큰(지번) 추출
        tokens = keyword.strip().split()
        if not tokens:
            return None
        last = re.sub(r"번지$", "", tokens[-1])
        if not re.match(r"^\d+(-\d+)?$", last):
            return None  # 지번 토큰 아님 → 정확매칭 불가

        for j in juso_list:
            lnbr_main = str(j.get("lnbrMnnm", ""))
            lnbr_sub = str(j.get("lnbrSlno", "0"))
            jibun = lnbr_main if lnbr_sub == "0" else f"{lnbr_main}-{lnbr_sub}"
            buld_main = str(j.get("buldMnnm", ""))
            buld_sub = str(j.get("buldSlno", "0"))
            road = buld_main if buld_sub == "0" else f"{buld_main}-{buld_sub}"
            if last == jibun or last == road:
                pk = j.get("bdMgtSn")
                if pk:
                    return pk
        return None  # 정확매칭 없음

    # ---------- Juso (건물명 모드, 회수 단계 6에서 사용) ----------
    def search_juso_by_name(self, keyword, per_page=30):
        """Juso 검색 결과 전체를 리스트로 반환 (필터링은 호출 측에서)."""
        if not keyword or not self.juso_key:
            return []
        try:
            self._gw.wait("juso_addr_link")
            r = requests.get(
                self._juso_url,
                params={
                    "confmKey": self.juso_key,
                    "currentPage": 1,
                    "countPerPage": per_page,
                    "keyword": keyword,
                    "resultType": "json",
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return []
        if data.get("results", {}).get("common", {}).get("errorCode") != "0":
            return []
        return data.get("results", {}).get("juso") or []


# ============================================================
# 회수 단계 5: bdmgt_sn → jibun_addr 역추출
# ============================================================
class JibunDeriver:
    """bdmgt_sn(25자리)에서 jibun_addr를 역추출한다.

    bdMgtSn 구조: 시군구코드(5) + 법정동코드(5) + 대지구분(1)
                  + 본번(4) + 부번(4) + 일련번호(6)

    동작:
        1. 기존 정상행에서 (시군구5, 법정동5) → (구, 동) 매핑 추출
        2. 대상 행을 디코딩 → 매핑된 (구, 동) + (본번, 부번)으로 jibun 조립
        3. loc_addr의 구·동과 비교해 신뢰도 분류
            match              구·동 모두 일치 (가장 안전)
            dong_mismatch      구는 같으나 동이 다름 (마곡지구·행정동/법정동 차이로 흔함)
            no_code_map        매핑 풀에 없는 코드 — loc에 동이 있으면 loc로 보완
            gu_mismatch        구가 다름 (잘못된 bdmgt 의심 — 채택 안 함)

    DB 행의 PK·주소 컬럼명은 파이프라인마다 다르므로 생성 시 주입한다.
    """

    # 정규식은 patterns 모듈에서 공유 (기존 클래스 속성 호환 위해 별칭 유지)
    GU_DONG_RE = patterns.JIBUN_GU_DONG_RE
    GU_RE = patterns.JIBUN_GU_RE
    GU_DONG_PAREN_RE = patterns.GU_DONG_PAREN_RE

    def __init__(self, db, *, row_id_key, loc_key):
        self.db = db
        self._row_id_key = row_id_key
        self._loc_key = loc_key
        self.code_map = self._build_code_map()

    def _build_code_map(self):
        counts = {}
        for r in self.db.select_code_to_loc():
            m = self.GU_DONG_RE.search(r["jibun_addr"])
            if not m:
                continue
            pk = r["bdmgt_sn"]
            key = (pk[:5], pk[5:10])
            counts.setdefault(key, Counter())[(m.group(1), m.group(2))] += 1
        return {k: cnt.most_common(1)[0][0] for k, cnt in counts.items()}

    def _parse_loc(self, loc):
        if not loc:
            return None, None
        m = self.GU_DONG_RE.search(loc)
        if m:
            return m.group(1), m.group(2)
        m = self.GU_DONG_PAREN_RE.search(loc)
        if m:
            return m.group(1), m.group(2)
        m = self.GU_RE.search(loc)
        return (m.group(1) if m else None), None

    def derive(self, pk, loc_addr):
        sg, bj, plat = pk[:5], pk[5:10], pk[10]
        bun = pk[11:15].lstrip("0") or "0"
        ji = pk[15:19].lstrip("0") or "0"
        loc_gu, loc_dong = self._parse_loc(loc_addr)

        # 매핑에 없으면 loc의 (구,동)으로 보완 시도
        if (sg, bj) not in self.code_map:
            if loc_gu and loc_dong:
                san = "산 " if plat == "2" else ""
                jibun = f"서울 {loc_gu} {loc_dong} {san}{bun}" + (f"-{ji}" if ji != "0" else "")
                return jibun, "no_code_map_loc_fill"
            return None, "no_code_map"

        gu, dong = self.code_map[(sg, bj)]
        san = "산 " if plat == "2" else ""
        jibun = f"서울 {gu} {dong} {san}{bun}" + (f"-{ji}" if ji != "0" else "")

        if loc_gu and loc_gu != gu:
            return jibun, "gu_mismatch"
        if loc_dong and loc_dong != dong:
            return jibun, "dong_mismatch"
        return jibun, "match"

    def run(self, accept_dong_mismatch=True):
        """기본은 match + no_code_map_loc_fill + dong_mismatch까지 채택.
        gu_mismatch는 채택 안 함 (잘못된 bdmgt 의심)."""
        rows = self.db.select_with_bdmgt_no_jibun()
        print(f"  역추출 대상: {len(rows)}건")
        stat = Counter()
        applied = 0
        for r in rows:
            jibun, status = self.derive(r["bdmgt_sn"], r[self._loc_key])
            stat[status] += 1
            if jibun is None:
                continue
            if status == "gu_mismatch":
                continue
            if status == "dong_mismatch" and not accept_dong_mismatch:
                continue
            self.db.update_jibun(r[self._row_id_key], jibun)
            applied += 1
        self.db.commit()
        print(f"  분포: {dict(stat)}")
        print(f"  반영: {applied}건")


# ============================================================
# 회수 단계 6: 건물명 + 구 필터로 bdmgt_sn 단일+동일치 매칭
# ============================================================
class NameLookup:
    """bdmgt_sn 누락 행을 건물명 키워드로 juso에 질의한다.

    이전에 단순히 첫 결과 채택을 했더니 다른 구·다른 시도 매칭으로 잘못 채워지는
    사례가 있어, 다음 두 단계의 필터를 둔다:
        (a) sggNm(구)이 loc_addr의 구와 같은 결과만 후보
        (b) bdMgtSn 단위로 dedupe 후 단일이고 emdNm(동)까지 같은 결과만 채택

    DB 행의 PK·주소·건물명 컬럼명은 파이프라인마다 다르므로 생성 시 주입한다.
    """

    GU_DONG_RE = patterns.NAME_GU_DONG_RE
    GU_RE = patterns.NAME_GU_RE

    def __init__(self, db, enricher, *, row_id_key, loc_key, name_key):
        self.db = db
        self.enricher = enricher
        self._row_id_key = row_id_key
        self._loc_key = loc_key
        self._name_key = name_key

    def _parse(self, loc):
        if not loc:
            return None, None
        m = self.GU_DONG_RE.search(loc)
        if m:
            return m.group(1), m.group(2)
        m = self.GU_RE.search(loc)
        return (m.group(1) if m else None), None

    def run(self):
        rows = self.db.select_missing_bdmgt()
        # 의미있는 검색을 위해 이름 최소 길이
        targets = [r for r in rows if r[self._name_key] and len(r[self._name_key].strip()) >= 3]
        print(f"  이름조회 대상: {len(targets)}건")

        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))

        stat = Counter()
        applied = 0
        for i, r in enumerate(targets, 1):
            loc_gu, loc_dong = self._parse(r[self._loc_key])
            if not loc_gu:
                stat["no_gu"] += 1
                continue
            juso_results = self.enricher.search_juso_by_name(r[self._name_key].strip())
            if not juso_results:
                stat["no_result"] += 1
                continue

            uniq_dong = {}
            for j in juso_results:
                if j.get("sggNm") != loc_gu:
                    continue
                if loc_dong and j.get("emdNm") != loc_dong:
                    continue
                pk = j.get("bdMgtSn")
                if pk:
                    uniq_dong.setdefault(pk, j)

            if len(uniq_dong) == 1:
                pk = next(iter(uniq_dong))
                self.db.update_bdmgt(r[self._row_id_key], pk)
                applied += 1
                stat["single_dong"] += 1
            elif len(uniq_dong) > 1:
                stat["multi_dong"] += 1
            else:
                stat["no_match"] += 1

            if i % 100 == 0:
                self.db.commit()
                print(f"    {i}/{len(targets)} | applied={applied}")
            if stop["v"]:
                break

        self.db.commit()
        print(f"  분포: {dict(stat)}")
        print(f"  반영: {applied}건")
