"""gseed.db 전체 파이프라인.

이 파일 하나로 0에서 gseed.db(녹색건축 G-SEED 인증현황)를 만들 수 있다.

단계:
    1. 공공데이터포털 G-SEED API 호출 → 메모리 수집 (페이지네이션)
    2. 서울 + 비주거(공동주택·학교 등 제외) 필터 + 정제 후 SQLite 적재
    3. 카카오 주소검색 API → 정밀 지번주소(jibun_addr) 채움
    4. 도로명주소(Juso) API → 건물관리번호(bdmgt_sn) 채움
    5. [회수] bdmgt_sn → jibun_addr 역추출 (jibun 누락이지만 bdmgt는 있는 행)
    6. [회수] 건물명+구 필터로 bdmgt_sn 단일+동일치 매칭
    7. [회수] 건물명 키워드검색 → jibun_addr (자동 매칭 + 확인된 건 override)

실행:
    1) .env에 세 API 키가 있어야 한다.
       GSEED_API_KEY          공공데이터포털 odcloud (G-SEED 인증현황)
       KAKAO_API_KEY   카카오 디벨로퍼스 REST 키
       JUSO_API_KEY         도로명주소 안내시스템 confmKey

    2) python gseed_pipeline.py            전체 빌드
       python gseed_pipeline.py --fresh    기존 DB 삭제 후 처음부터
       python gseed_pipeline.py --skip-kakao --skip-juso
                                          회수 단계만 (이미 ETL은 끝난 DB에서)

설계 의도:
    - efficiency_pipeline.py와 동일한 단계 구조.
    - DB 접근은 GseedDB로 모은다. SQL이 한 곳에만 있다.
    - 외부 API 호출은 GseedApiClient / AddressEnricher가 캡슐화.
    - 회수(JibunDeriver, NameLookup)는 별도 클래스. 본 ETL 후에 따로 켜고 끌 수 있다.
    - 모든 단계는 idempotent — 이미 채워진 행은 건너뛴다. 재실행 안전.
"""

import argparse
import os
import re
import signal
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


# ============================================================
# 설정·상수
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
# 프로젝트 루트의 .env에서 키 로드
load_dotenv(PROJECT_ROOT / ".env")
# infra.gateway import를 위해 프로젝트 루트를 sys.path에 추가
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from infra.gateway import get_default as _get_gateway  # noqa: E402

_GW = _get_gateway()

DB_PATH = SCRIPT_DIR.parent / "DB" / "building_certifications.db"

GSEED_API_KEY = os.getenv("GSEED_API_KEY")
KAKAO_KEY = os.getenv("KAKAO_API_KEY")
JUSO_KEY = os.getenv("JUSO_API_KEY")

# 엔드포인트
GSEED_URL = "https://api.odcloud.kr/api/15003214/v1/uddi:590f870b-8aef-4ac0-a08e-a36bbbdb01ec"
KAKAO_GEOCODE_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
JUSO_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

# 적재 필터 — 서울 + 비주거(주거·학교 카테고리 제외)
TARGET_PROVINCE = "서울특별시"
EXCLUDE_CATEGORIES = {
    "공동주택",
    "일반주택",
    "신축 주거(공동주택)",
    "신축 주거(일반주택)",
    "학교시설",
    "복합건축물(주거)",
    "신축 비주거(학교시설)",
}

# G-SEED API 응답 한글키 → 영문 컬럼 매핑
# (API 응답이 한글 키이고, 가끔 "건축물 주소"/"건축물주소"/"주소"로 변형되어 옴)
FIELD_MAP = {
    "cert_no":           ["인증번호"],
    "province":          ["시도"],
    "cert_standard":     ["인증기준"],
    "cert_type":         ["인증구분"],
    "building_name":     ["건축물명"],
    "building_category": ["인증대상 건축물군", "인증대상건축물군"],
    "cert_grade":        ["인증등급"],
    "cert_date":         ["인증일자"],
    "address":           ["건축물 주소", "건축물주소", "주소"],
    "total_floor_area":  ["연면적"],
    "floors_above":      ["지상층수"],
    "floors_below":      ["지하층수"],
}

# 호출 조절
# 호출 간격은 infra/gateway/rate_limits.json이 중앙 관리 (kakao_local, juso_addr_link, gseed).
# ThreadPool 동시 호출 수: 게이트웨이가 직렬화하므로 worker가 늘어도 quota는 보호됨.
GSEED_PAGE_SIZE = 1000
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
BATCH_COMMIT_EVERY = 50
KAKAO_WORKERS = 8
JUSO_WORKERS = 4

WS_RE = re.compile(r"\s+")


def _clean(text):
    """문자열 strip + 다중 공백 단일화. None 안전."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    return WS_RE.sub(" ", s)


def _pick(raw, key_list):
    """한글키 후보 리스트에서 첫 번째 유효 값 반환."""
    for k in key_list:
        v = raw.get(k)
        if v is not None and v != "":
            return v
    return None


# ============================================================
# 단계 1: G-SEED API 호출
# ============================================================
class GseedApiClient:
    """공공데이터포털 odcloud G-SEED 인증현황 API를 페이지네이션으로 전건 수집한다.

    응답 키는 한글 ("인증번호", "건축물명", ...). 메모리에 dict 리스트로 모은다.
    네트워크/일시오류는 지수 백오프로 재시도. 페이지 사이 sleep으로 서버 부담을 줄인다.
    """

    def __init__(self, service_key, page_size=GSEED_PAGE_SIZE):
        if not service_key:
            raise SystemExit("GSEED_API_KEY가 .env에 없습니다.")
        self.service_key = service_key
        self.page_size = page_size

    def fetch_all(self, limit_pages=0):
        """전건 수집. limit_pages>0이면 그만큼만 (개발용)."""
        rows = []
        page = 1
        while True:
            data = self._fetch_page(page)
            page_rows = data.get("data", [])
            total = int(data.get("totalCount", 0) or 0)
            if not page_rows:
                break
            rows.extend(page_rows)
            print(f"    page {page}: {len(page_rows)}건 → 누적 {len(rows):,}/{total:,}")
            if len(rows) >= total:
                break
            if limit_pages > 0 and page >= limit_pages:
                break
            page += 1
        return rows

    def _fetch_page(self, page_no):
        params = {
            "page": page_no,
            "perPage": self.page_size,
            "serviceKey": self.service_key,
            "returnType": "JSON",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                _GW.wait("gseed")
                r = requests.get(GSEED_URL, params=params, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError):
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(2 ** attempt)


# ============================================================
# 단계 2 보조: 행 정제 (서울+비주거 필터, 숫자 변환)
# ============================================================
class RowTransformer:
    """G-SEED 원본 dict 한 건을 DB 적재용 dict로 변환.

    None을 반환하면 적재 대상 외 (필터 탈락).
    """

    @staticmethod
    def transform(raw):
        province = _clean(_pick(raw, FIELD_MAP["province"]))
        category = _clean(_pick(raw, FIELD_MAP["building_category"]))

        # 필터: 서울 + 비주거(공동주택/학교 등 제외)
        if province != TARGET_PROVINCE:
            return None
        if category in EXCLUDE_CATEGORIES:
            return None

        cert_no = _clean(_pick(raw, FIELD_MAP["cert_no"]))
        if not cert_no:
            return None

        bld_nm = _clean(_pick(raw, FIELD_MAP["building_name"]))
        addr = _clean(_pick(raw, FIELD_MAP["address"]))
        if not bld_nm or not addr:
            return None

        return {
            "cert_no": cert_no,
            "province": province,
            "cert_standard": _clean(_pick(raw, FIELD_MAP["cert_standard"])) or "",
            "cert_type": _clean(_pick(raw, FIELD_MAP["cert_type"])) or "",
            "building_name": bld_nm,
            "building_category": category or "",
            "cert_grade": _clean(_pick(raw, FIELD_MAP["cert_grade"])) or "",
            "cert_date": _clean(_pick(raw, FIELD_MAP["cert_date"])) or "",
            "address": addr,
            "total_floor_area": RowTransformer._safe_float(_pick(raw, FIELD_MAP["total_floor_area"])),
            "floors_above": RowTransformer._safe_int(_pick(raw, FIELD_MAP["floors_above"])),
            "floors_below": RowTransformer._safe_int(_pick(raw, FIELD_MAP["floors_below"])),
        }

    @staticmethod
    def _safe_int(v):
        if v is None or v == "":
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


# ============================================================
# 단계 3·4 보조: 카카오 + Juso 호출 (주소 enrich)
# ============================================================
class AddressEnricher:
    """카카오 주소검색으로 jibun_addr, Juso로 bdmgt_sn을 채운다.

    두 API 모두 단건 호출 함수를 제공한다. 호출 실패·결과 없음은 None 반환
    (예외를 던지지 않음). 호출 측이 None을 보고 적절히 처리한다.
    """

    def __init__(self, kakao_key, juso_key):
        self.kakao_key = kakao_key
        self.juso_key = juso_key

    def fetch_jibun_addr(self, raw_addr):
        """카카오 주소검색 → 정규화된 지번주소. 실패 시 None."""
        if not raw_addr or not self.kakao_key:
            return None
        try:
            _GW.wait("kakao_local")
            r = requests.get(
                KAKAO_GEOCODE_URL,
                params={"query": raw_addr},
                headers={"Authorization": f"KakaoAK {self.kakao_key}"},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        docs = data.get("documents") or []
        if not docs:
            return None
        addr = (docs[0].get("address") or {})
        return addr.get("address_name") or None

    def search_keyword(self, query):
        """카카오 키워드(장소)검색 → 후보 목록. 실패 시 []."""
        if not query or not self.kakao_key:
            return []
        try:
            _GW.wait("kakao_local")
            r = requests.get(
                KAKAO_KEYWORD_URL,
                params={"query": query, "size": 15},
                headers={"Authorization": f"KakaoAK {self.kakao_key}"},
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return []
        return data.get("documents") or []

    def fetch_bdmgt_sn(self, keyword):
        """Juso API → 25자리 건물관리번호. 마지막 토큰(지번) 정확매칭만 채택."""
        if not keyword or not self.juso_key:
            return None
        try:
            _GW.wait("juso_addr_link")
            r = requests.get(
                JUSO_URL,
                params={
                    "confmKey": self.juso_key,
                    "currentPage": 1,
                    "countPerPage": 10,
                    "keyword": keyword,
                    "resultType": "json",
                },
                timeout=REQUEST_TIMEOUT,
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

        tokens = keyword.strip().split()
        if not tokens:
            return None
        last = re.sub(r"번지$", "", tokens[-1])
        if not re.match(r"^\d+(-\d+)?$", last):
            return None

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
        return None

    def search_juso_by_name(self, keyword, per_page=30):
        """Juso 검색 전체 결과 반환 (필터링은 호출 측에서)."""
        if not keyword or not self.juso_key:
            return []
        try:
            _GW.wait("juso_addr_link")
            r = requests.get(
                JUSO_URL,
                params={
                    "confmKey": self.juso_key,
                    "currentPage": 1,
                    "countPerPage": per_page,
                    "keyword": keyword,
                    "resultType": "json",
                },
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return []
        if data.get("results", {}).get("common", {}).get("errorCode") != "0":
            return []
        return data.get("results", {}).get("juso") or []


# ============================================================
# DB 액세스
# ============================================================
class GseedDB:
    """gseed.db 스키마 생성·INSERT·조회를 모은다.

    SQL을 한 곳에만 두어 다른 단계가 SQL 디테일을 모르게 한다.
    v_latest_cert 뷰: (주소+건물명) 그룹별 최신 인증 1건만 노출.
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def create_schema(self):
        c = self.conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS gseed_certifications (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                cert_no           TEXT NOT NULL,
                province          TEXT NOT NULL,
                cert_standard     TEXT NOT NULL,
                cert_type         TEXT NOT NULL,
                building_name     TEXT NOT NULL,
                building_category TEXT NOT NULL,
                cert_grade        TEXT NOT NULL,
                cert_date         TEXT NOT NULL,
                address           TEXT NOT NULL,
                total_floor_area  REAL,
                floors_above      INTEGER,
                floors_below      INTEGER,
                jibun_addr        TEXT,
                bdmgt_sn          TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_address       ON gseed_certifications(address);
            CREATE INDEX IF NOT EXISTS idx_building_name ON gseed_certifications(building_name);
            CREATE INDEX IF NOT EXISTS idx_cert_no       ON gseed_certifications(cert_no);

            CREATE VIEW IF NOT EXISTS v_latest_cert AS
            WITH ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY address, building_name
                        ORDER BY
                            CASE cert_type
                                WHEN '유효기간연장(본인증)' THEN 1
                                WHEN '본인증' THEN 2
                                WHEN '예비인증' THEN 3
                                ELSE 99
                            END,
                            cert_date DESC
                    ) AS rn
                FROM gseed_certifications
            )
            SELECT id, cert_no, province, cert_standard, cert_type, building_name,
                   building_category, cert_grade, cert_date, address,
                   total_floor_area, floors_above, floors_below
            FROM ranked WHERE rn = 1;
        """)
        self.conn.commit()

    def replace_all(self, rows):
        """기존 전체 삭제 후 일괄 INSERT (재실행 시 깨끗하게 다시 적재)."""
        self.conn.execute("DELETE FROM gseed_certifications")
        self.conn.executemany("""
            INSERT INTO gseed_certifications
                (cert_no, province, cert_standard, cert_type, building_name,
                 building_category, cert_grade, cert_date, address,
                 total_floor_area, floors_above, floors_below)
            VALUES (:cert_no, :province, :cert_standard, :cert_type, :building_name,
                    :building_category, :cert_grade, :cert_date, :address,
                    :total_floor_area, :floors_above, :floors_below)
        """, rows)
        self.conn.commit()

    def select_missing_jibun(self):
        return self.conn.execute("""
            SELECT id, address FROM gseed_certifications
             WHERE (jibun_addr IS NULL OR jibun_addr = '')
             ORDER BY id
        """).fetchall()

    def select_missing_bdmgt(self):
        return self.conn.execute("""
            SELECT id, building_name, address, jibun_addr FROM gseed_certifications
             WHERE (bdmgt_sn IS NULL OR bdmgt_sn = '')
             ORDER BY id
        """).fetchall()

    def select_with_bdmgt_no_jibun(self):
        return self.conn.execute("""
            SELECT id, building_name, address, bdmgt_sn FROM gseed_certifications
             WHERE (jibun_addr IS NULL OR jibun_addr = '')
               AND bdmgt_sn IS NOT NULL AND length(bdmgt_sn) = 25
             ORDER BY id
        """).fetchall()

    def select_code_to_loc(self):
        return self.conn.execute("""
            SELECT jibun_addr, bdmgt_sn FROM gseed_certifications
             WHERE bdmgt_sn IS NOT NULL AND length(bdmgt_sn) = 25
               AND jibun_addr IS NOT NULL AND jibun_addr <> ''
        """).fetchall()

    def update_jibun(self, row_id, jibun):
        self.conn.execute(
            "UPDATE gseed_certifications SET jibun_addr=? WHERE id=? "
            "AND (jibun_addr IS NULL OR jibun_addr='')",
            (jibun, row_id),
        )

    def update_bdmgt(self, row_id, bdmgt):
        self.conn.execute(
            "UPDATE gseed_certifications SET bdmgt_sn=? WHERE id=? "
            "AND (bdmgt_sn IS NULL OR bdmgt_sn='')",
            (bdmgt, row_id),
        )

    def commit(self):
        self.conn.commit()

    def stats(self):
        row = self.conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN jibun_addr IS NULL OR jibun_addr=''
                            THEN 1 ELSE 0 END) AS no_jibun,
                   SUM(CASE WHEN bdmgt_sn IS NULL OR bdmgt_sn=''
                            THEN 1 ELSE 0 END) AS no_bdmgt
              FROM gseed_certifications
        """).fetchone()
        return dict(row)


# ============================================================
# 회수 단계 5: bdmgt_sn → jibun_addr 역추출
# ============================================================
class JibunDeriver:
    """bdmgt_sn(25자리)에서 jibun_addr를 역추출한다.

    bdMgtSn 구조: 시군구코드(5) + 법정동코드(5) + 대지구분(1)
                  + 본번(4) + 부번(4) + 일련번호(6)
    """

    GU_DONG_RE = re.compile(r"서울\S*\s+(\S+구)\s+([가-힣]+[가-힣\d]*(?:동|가|리)\S*)")
    GU_RE = re.compile(r"서울\S*\s+(\S+구)")
    GU_DONG_PAREN_RE = re.compile(r"(\S+구)\b[^()]*\(([가-힣]+\d*가?)\)")

    def __init__(self, db):
        self.db = db
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
        rows = self.db.select_with_bdmgt_no_jibun()
        print(f"  역추출 대상: {len(rows)}건")
        stat = Counter()
        applied = 0
        for r in rows:
            jibun, status = self.derive(r["bdmgt_sn"], r["address"])
            stat[status] += 1
            if jibun is None:
                continue
            if status == "gu_mismatch":
                continue
            if status == "dong_mismatch" and not accept_dong_mismatch:
                continue
            self.db.update_jibun(r["id"], jibun)
            applied += 1
        self.db.commit()
        print(f"  분포: {dict(stat)}")
        print(f"  반영: {applied}건")


# ============================================================
# 회수 단계 6: 건물명 + 구 필터로 bdmgt_sn 단일+동일치 매칭
# ============================================================
class NameLookup:
    """bdmgt_sn 누락 행을 building_name으로 juso에 질의.

    (a) sggNm(구) 일치
    (b) bdMgtSn 단위로 dedupe 후 단일이고 emdNm(동)까지 일치한 결과만 채택
    이렇게 두 단계 필터로 거짓양성 매칭을 차단한다.
    """

    GU_DONG_RE = re.compile(r"(?:서울\S*\s*)?(\S+구)\s+([가-힣]+[가-힣\d]*(?:동|가|리)\S*)")
    GU_RE = re.compile(r"(?:서울\S*\s*)?(\S+구)")

    def __init__(self, db, enricher):
        self.db = db
        self.enricher = enricher

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
        targets = [r for r in rows if r["building_name"] and len(r["building_name"].strip()) >= 3]
        print(f"  이름조회 대상: {len(targets)}건")

        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))

        stat = Counter()
        applied = 0
        for i, r in enumerate(targets, 1):
            loc_gu, loc_dong = self._parse(r["address"])
            if not loc_gu:
                stat["no_gu"] += 1
                continue
            juso_results = self.enricher.search_juso_by_name(r["building_name"].strip())
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
                self.db.update_bdmgt(r["id"], pk)
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


# ============================================================
# 회수 단계 7: 건물명 키워드검색 → jibun_addr
# ============================================================
class NameJibunLookup:
    """bdmgt는 있으나 jibun이 빈 행을 building_name으로 카카오 키워드검색.

    카카오 주소검색(단계 3)·bdmgt 역추출(단계 5)로도 못 채운 행을
    장소(키워드)검색으로 회수한다. 거짓양성을 막기 위해 구(區) 일치를
    전제로, (a) 장소명이 건물명과 겹치거나 (b) 결과 지번이 원문 지번과
    일치할 때만 채택한다. 건물명에 박힌 지번과 결과가 어긋나면 거부한다.

    OVERRIDE: 자동 규칙으로는 이름이 어긋나 못 잡지만 사람이 확인한 건.
    재빌드 시 id가 바뀌므로 영구 유일키인 bdmgt_sn으로 키잉한다.
    """

    OVERRIDE = {
        "1150010500100000000000008": "서울 강서구 마곡동 745",   # 서울남부출입국관리사무소
        "1150010100102800015028362": "서울 강서구 염창동 310",   # 염창아네스트
        "1111017500114200000000822": "서울 종로구 숭인동 1420",  # 숭인동 1420
    }

    def __init__(self, db, enricher):
        self.db = db
        self.enricher = enricher

    @staticmethod
    def _gu(address):
        for t in address.split():
            if t.endswith(("구", "군")):
                return t
        return ""

    @staticmethod
    def _sido(address):
        toks = address.split()
        return toks[0] if toks else ""

    @staticmethod
    def _core_tokens(name):
        # 매칭용 핵심 토큰 (괄호·로마숫자·구분기호 제거)
        name = re.sub(r"[\(\[].*?[\)\]]", " ", name)
        name = re.sub(r"[ⅠⅡⅢ()\[\]·,/]", " ", name)
        return [t for t in name.split() if len(t) >= 2]

    @staticmethod
    def _real_jibun(text):
        # '12-3' 하이픈형 또는 '123번지'만 진짜 지번으로 인정 (3차·D16 등 제외)
        m = re.search(r"\d+-\d+|\d+(?=번지)", text)
        return m.group() if m else None

    def _pick(self, docs, sido, gu, bname):
        # 구 일치 후보 중 이름이 겹치면 확신, 아니면 첫 후보를 검토용으로 보관
        toks = self._core_tokens(bname)
        best = None
        for d in docs:
            addr = d.get("address_name", "")
            place = d.get("place_name", "")
            if not (gu and gu in addr and (not sido or sido[:2] in addr)):
                continue
            overlap = (any(t in place for t in toks)
                       or any(t in bname for t in self._core_tokens(place)))
            if overlap:
                return addr, True
            if best is None:
                best = (addr, False)
        return best if best else (None, None)

    def run(self):
        rows = self.db.select_with_bdmgt_no_jibun()
        targets = [r for r in rows
                   if r["building_name"] and len(r["building_name"].strip()) >= 3]
        print(f"  이름검색 대상: {len(targets)}건")

        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))

        stat = Counter()
        applied = 0
        for i, r in enumerate(targets, 1):
            pk = r["bdmgt_sn"]
            if pk in self.OVERRIDE:
                self.db.update_jibun(r["id"], self.OVERRIDE[pk])
                applied += 1
                stat["override"] += 1
                continue

            bname = r["building_name"].strip()
            address = r["address"] or ""
            sido, gu = self._sido(address), self._gu(address)

            result = confident = None
            for q in (bname, f"{gu} {bname}".strip()):
                docs = self.enricher.search_keyword(q)
                result, confident = self._pick(docs, sido, gu, bname)
                if confident:
                    break

            # 건물명 지번이 결과와 어긋나면 오매칭 → 거부, 일치하면 확신 승격
            name_jibun = self._real_jibun(bname)
            addr_jibun = self._real_jibun(address)
            tail = re.search(r"\d+-\d+|\d+$", result or "")
            tail = tail.group() if tail else None
            conflict = bool(name_jibun and tail and name_jibun != tail)
            jibun_ok = bool(tail and tail in (name_jibun, addr_jibun))

            if result and not conflict and (confident or jibun_ok):
                self.db.update_jibun(r["id"], result)
                applied += 1
                stat["match"] += 1
            elif result:
                stat["review"] += 1
            else:
                stat["miss"] += 1

            if i % BATCH_COMMIT_EVERY == 0:
                self.db.commit()
                print(f"    {i}/{len(targets)} | applied={applied}")
            if stop["v"]:
                break

        self.db.commit()
        print(f"  분포: {dict(stat)}")
        print(f"  반영: {applied}건")


# ============================================================
# 파이프라인 조립
# ============================================================
class GseedPipeline:
    """전체 단계를 순서대로 실행한다."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.client = GseedApiClient(GSEED_API_KEY)
        self.enricher = AddressEnricher(KAKAO_KEY, JUSO_KEY)

    def run(
        self,
        fresh=False,
        limit_api_pages=0,
        skip_kakao=False,
        skip_juso=False,
        skip_recovery=False,
    ):
        # building_certifications.db는 efficiency 파이프라인과 공유하므로 파일 자체는
        # 지우지 않고 자기 테이블(gseed_certifications)만 비운다.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if fresh and self.db_path.exists():
            print(f"[0] 기존 gseed_certifications 테이블 삭제")
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DROP TABLE IF EXISTS gseed_certifications")

        db = GseedDB(self.db_path)
        db.create_schema()

        if not skip_kakao and not skip_juso:
            print("[1] G-SEED API 수집")
            raw_rows = self.client.fetch_all(limit_pages=limit_api_pages)
            print(f"[2] 정제·서울·비주거 필터 + 적재")
            transformed = []
            skipped = 0
            for raw in raw_rows:
                row = RowTransformer.transform(raw)
                if row is None:
                    skipped += 1
                    continue
                transformed.append(row)
            print(f"  적재 대상 {len(transformed):,}건 (필터 탈락 {skipped:,}건)")
            db.replace_all(transformed)

        if not skip_kakao:
            print("[3] 카카오 주소검색 → jibun_addr")
            self._fill_jibun(db)

        if not skip_juso:
            print("[4] Juso 도로명주소 → bdmgt_sn")
            self._fill_bdmgt(db)

        if not skip_recovery:
            print("[5] 회수: bdmgt_sn → jibun_addr 역추출")
            JibunDeriver(db).run()

            print("[6] 회수: 건물명 + 구 필터로 bdmgt_sn 단일+동일치 매칭")
            NameLookup(db, self.enricher).run()

            print("[7] 회수: 건물명 키워드검색 → jibun_addr")
            NameJibunLookup(db, self.enricher).run()

        s = db.stats()
        print("\n=== 최종 상태 ===")
        print(f"  total      : {s['total']:,}")
        print(f"  no_jibun   : {s['no_jibun']:,}")
        print(f"  no_bdmgt   : {s['no_bdmgt']:,}")
        db.close()

    def _fill_jibun(self, db):
        from concurrent.futures import ThreadPoolExecutor

        rows = db.select_missing_jibun()
        print(f"  카카오 대상: {len(rows)}건")
        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))

        def lookup(r):
            if stop["v"]:
                return r["id"], None
            return r["id"], self.enricher.fetch_jibun_addr(r["address"])

        ok = miss = 0
        with ThreadPoolExecutor(max_workers=KAKAO_WORKERS) as ex:
            for i, (row_id, jibun) in enumerate(ex.map(lookup, rows), 1):
                if jibun:
                    db.update_jibun(row_id, jibun)
                    ok += 1
                else:
                    miss += 1
                if i % BATCH_COMMIT_EVERY == 0:
                    db.commit()
                    print(f"    {i}/{len(rows)} | ok={ok} miss={miss}")
                if stop["v"]:
                    break
        db.commit()
        print(f"  완료: ok={ok}, miss={miss}")

    def _fill_bdmgt(self, db):
        from concurrent.futures import ThreadPoolExecutor

        rows = db.select_missing_bdmgt()
        print(f"  Juso 대상: {len(rows)}건")
        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))

        def lookup(r):
            keyword = r["jibun_addr"] or r["address"]
            if not keyword or stop["v"]:
                return r["id"], None
            return r["id"], self.enricher.fetch_bdmgt_sn(keyword)

        ok = miss = 0
        with ThreadPoolExecutor(max_workers=JUSO_WORKERS) as ex:
            for i, (row_id, pk) in enumerate(ex.map(lookup, rows), 1):
                if pk:
                    db.update_bdmgt(row_id, pk)
                    ok += 1
                else:
                    miss += 1
                if i % BATCH_COMMIT_EVERY == 0:
                    db.commit()
                    print(f"    {i}/{len(rows)} | ok={ok} miss={miss}")
                if stop["v"]:
                    break
        db.commit()
        print(f"  완료: ok={ok}, miss={miss}")


# ============================================================
# 진입점
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="gseed.db 전체 파이프라인 빌드")
    parser.add_argument("--fresh", action="store_true",
                        help="기존 DB 삭제 후 처음부터 빌드")
    parser.add_argument("--limit-api", type=int, default=0,
                        help="G-SEED API 페이지 수 제한 (개발용, 0=전체)")
    parser.add_argument("--skip-kakao", action="store_true",
                        help="3단계(카카오 jibun) 건너뜀")
    parser.add_argument("--skip-juso", action="store_true",
                        help="4단계(Juso bdmgt) 건너뜀")
    parser.add_argument("--skip-recovery", action="store_true",
                        help="5·6단계(회수) 건너뜀")
    args = parser.parse_args()

    if not GSEED_API_KEY:
        print("[경고] GSEED_API_KEY가 없습니다. 1·2단계는 실패합니다.", file=sys.stderr)
    if not KAKAO_KEY and not args.skip_kakao:
        print("[경고] KAKAO_API_KEY가 없습니다. 3단계는 건너뛰는 것을 권장합니다.", file=sys.stderr)
    if not JUSO_KEY and not (args.skip_juso and args.skip_recovery):
        print("[경고] JUSO_API_KEY가 없습니다. 4·6단계는 건너뛰는 것을 권장합니다.", file=sys.stderr)

    pipeline = GseedPipeline()
    pipeline.run(
        fresh=args.fresh,
        limit_api_pages=args.limit_api,
        skip_kakao=args.skip_kakao,
        skip_juso=args.skip_juso,
        skip_recovery=args.skip_recovery,
    )


if __name__ == "__main__":
    main()
