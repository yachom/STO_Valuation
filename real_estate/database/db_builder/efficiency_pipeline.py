"""efficiency.db 전체 파이프라인.

이 파일 하나로 0에서 efficiency.db를 만들 수 있다.

단계:
    1. BEEC API 호출 (공공데이터포털 건축물에너지효율등급 인증현황) → 메모리 수집
    2. 서울 + 비주거 필터 + 등급코드/이상치 정제 후 SQLite 적재
    3. 카카오 주소검색 API → 정밀 지번주소(jibun_addr) 채움
    4. 도로명주소(Juso) API → 건물관리번호(bdmgt_sn) 채움
    5. [회수] bdmgt_sn → jibun_addr 역추출 (jibun 누락이지만 bdmgt는 있는 행)
    6. [회수] 건물명+구 필터로 bdmgt_sn 단일+동일치 매칭 (이름검색 거짓양성 차단)
    7. [회수] 건물명 키워드검색 → jibun_addr (구 일치 + 이름/지번 검증)

실행:
    1) .env에 세 API 키가 있어야 한다.
       BEEC_API_KEY  공공데이터포털 BEEC 활용신청 키
       KAKAO_API_KEY  카카오 디벨로퍼스 REST 키
       JUSO_API_KEY        도로명주소 안내시스템 confmKey

    2) python efficiency_pipeline.py            전체 빌드
       python efficiency_pipeline.py --fresh    기존 DB 삭제 후 처음부터
       python efficiency_pipeline.py --skip-kakao --skip-juso
                                                회수 단계만 (이미 ETL은 끝난 DB에서)

설계 의도:
    - 단계마다 단일 책임의 클래스를 둔다. 다른 데이터셋에 같은 패턴을 쓰기 쉽다.
    - DB 접근은 EfficiencyDB로 모은다. SQL이 한 곳에만 있다.
    - 외부 API 호출은 BeecApiClient / AddressEnricher가 캡슐화.
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
load_dotenv(SCRIPT_DIR / ".env")

DB_PATH = SCRIPT_DIR.parent / "DB" / "building_certifications.db"

BEEC_KEY = os.getenv("BEEC_API_KEY")
KAKAO_KEY = os.getenv("KAKAO_API_KEY")
JUSO_KEY = os.getenv("JUSO_API_KEY")

# 엔드포인트
BEEC_URL = "http://apis.data.go.kr/B553530/BEEC/BEEC_01_LIST"
KAKAO_GEOCODE_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
JUSO_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

# 적재 필터
TARGET_REGION = "서울"
EXCLUDE_PURPOSE = "주거용"

# 이상치 임계 (단순 보호용 — 데이터 입력 오류 방어)
MAX_HSHLD = 30_000
MAX_AREA_M2 = 1_000_000

# 등급 라벨 → (코드, 순위)
GRADE_MAP = {
    "1+++등급": ("1+++", 1), "1++등급": ("1++", 2), "1+등급": ("1+", 3),
    "1등급":    ("1",   4),  "2등급":  ("2",  5), "3등급": ("3", 6),
    "4등급":    ("4",   7),  "5등급":  ("5",  8), "6등급": ("6", 9),
    "등급외":   ("OUT", 99),
}

# 호출 조절
BEEC_PAGE_SIZE = 100
KAKAO_SLEEP = 0.1
JUSO_SLEEP = 0.2
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
BATCH_COMMIT_EVERY = 50

# 공백 정규화
WS_RE = re.compile(r"\s+")


def _clean(text):
    """문자열 strip + 다중 공백 단일화. None 안전."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    return WS_RE.sub(" ", s)


# ============================================================
# 단계 1: BEEC API 호출
# ============================================================
class BeecApiClient:
    """공공데이터포털 BEEC_01_LIST API를 페이지네이션으로 전건 수집한다.

    응답 키는 영문 대문자 (RQID, BLD_NM, LOC_ADDR, ...). 메모리에 dict 리스트로 모은다.
    네트워크/일시오류는 지수 백오프로 재시도. 페이지 사이 sleep으로 서버 부담을 줄인다.
    """

    def __init__(self, api_key, page_size=BEEC_PAGE_SIZE):
        if not api_key:
            raise SystemExit("BEEC_API_KEY가 .env에 없습니다.")
        self.api_key = api_key
        self.page_size = page_size

    def fetch_all(self, limit_pages=0):
        """전건 수집. limit_pages>0이면 그만큼만 (개발용)."""
        head = self._fetch_page(page_no=1, num_of_rows=1)
        total = int(head["response"]["body"]["totalCount"])
        pages = (total + self.page_size - 1) // self.page_size
        if limit_pages > 0:
            pages = min(pages, limit_pages)
        print(f"  BEEC 전체 {total:,}건 / {pages}페이지 수집 시작")

        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = []
        for page in range(1, pages + 1):
            data = self._fetch_page(page, self.page_size)
            items = data["response"]["body"].get("items") or {}
            page_rows = items.get("item", []) if isinstance(items, dict) else []
            if isinstance(page_rows, dict):
                page_rows = [page_rows]
            for it in page_rows:
                it = dict(it)
                it["_fetched_at"] = fetched_at
                rows.append(it)
            if page % 20 == 0 or page == pages:
                print(f"    page {page}/{pages} → 누적 {len(rows):,}건")
            time.sleep(0.2)
        return rows

    def _fetch_page(self, page_no, num_of_rows=None):
        params = {
            "serviceKey": self.api_key,
            "pageNo": page_no,
            "numOfRows": num_of_rows or self.page_size,
            "apiType": "json",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = requests.get(BEEC_URL, params=params, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError):
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(2 ** attempt)


# ============================================================
# 단계 2 보조: 행 정제 (서울+비주거 필터, 등급 변환, 이상치 NULL)
# ============================================================
class RowTransformer:
    """BEEC 원본 dict 한 건을 DB 적재용 dict로 변환.

    None을 반환하면 적재 대상 외 (필터 탈락).
    """

    @staticmethod
    def transform(raw):
        rgn = _clean(raw.get("RGN_SCT_NAME"))
        purpose = _clean(raw.get("PURPOSE_KIND_NAME"))

        # 필터: 서울 + 비주거만 적재
        if rgn != TARGET_REGION:
            return None
        if purpose == EXCLUDE_PURPOSE:
            return None

        bld_nm = _clean(raw.get("BLD_NM"))
        if not bld_nm:
            return None  # 이름 없는 행은 무의미

        loc_addr = _clean(raw.get("LOC_ADDR"))
        full_addr = " ".join(filter(None, [rgn, loc_addr])) if (rgn or loc_addr) else None

        # 숫자 변환 — 빈문자열·None은 None으로
        hshld = RowTransformer._safe_int(raw.get("HSHLD_NUMB"))
        if hshld is not None and hshld > MAX_HSHLD:
            hshld = None  # 이상치 차단

        exus = RowTransformer._safe_float(raw.get("AL_EXUS_AREA"))
        if exus is not None and exus > MAX_AREA_M2:
            exus = None
        real = RowTransformer._safe_float(raw.get("W_REAL_AREA"))
        if real is not None and real > MAX_AREA_M2:
            real = None

        primary = RowTransformer._safe_float(raw.get("W_1ST_ENERGY_REQUIRE6"))
        reduce_rate = RowTransformer._safe_float(raw.get("AL_ENGY_REDU_RATE"))

        # 등급 라벨 → 코드/순위
        grd_name = _clean(raw.get("GRD_NAME"))
        grade_code = grade_rank = None
        if grd_name and grd_name in GRADE_MAP:
            grade_code, grade_rank = GRADE_MAP[grd_name]

        return {
            "rqid": int(raw["RQID"]),
            "crtif_no": _clean(raw.get("CRTIF_NO")),
            "bld_nm": bld_nm,
            "loc_addr": loc_addr,
            "rgn_sct_name": rgn,
            "full_addr": full_addr,
            "purpose_kind_name": purpose,
            "bld_scale": _clean(raw.get("W_BLD_SCALE")),
            "hshld_numb": hshld,
            "exus_area_m2": exus,
            "real_area_m2": real,
            "crtif_kind": _clean(raw.get("CRTIF_KIND")),
            "grd_name": grd_name,
            "grade_code": grade_code,
            "grade_rank": grade_rank,
            "crtif_isu_dd": _clean(raw.get("CRTIF_ISU_DD")),
            "app_rcpt_org_name": _clean(raw.get("APP_RCPT_ORG_NAME")),
            "ente_nm": _clean(raw.get("ENTE_NM")),
            "primary_energy_req": primary,
            "energy_reduce_rate": reduce_rate,
            "src_reg_dt": _clean(raw.get("DATA_REG_DT")),
            "fetched_at": raw.get("_fetched_at"),
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

    # ---------- 카카오 ----------
    def fetch_jibun_addr(self, raw_addr):
        """카카오 주소검색 → 정규화된 지번주소. 실패 시 None."""
        if not raw_addr or not self.kakao_key:
            return None
        try:
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
        first = docs[0]
        addr = first.get("address") or {}
        return addr.get("address_name") or None

    def search_keyword(self, query):
        """카카오 키워드(장소)검색 → 후보 목록. 실패 시 []."""
        if not query or not self.kakao_key:
            return []
        try:
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

    # ---------- Juso ----------
    def fetch_bdmgt_sn(self, keyword):
        """Juso API → 25자리 건물관리번호.

        검색 결과 중 입력 키워드의 마지막 토큰(지번 N 또는 N-N)과 본번/부번이
        정확히 일치하는 결과만 채택. 일치가 없으면 None (첫 결과 무작위 폴백 금지).
        """
        if not keyword or not self.juso_key:
            return None
        try:
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
class EfficiencyDB:
    """efficiency.db 스키마 생성·UPSERT·조회를 모은다.

    SQL을 한 곳에만 두어 다른 단계가 SQL 디테일을 모르게 한다.
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    # ---------- 스키마 ----------
    def create_schema(self):
        c = self.conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS building_energy_grade (
                rqid                INTEGER  PRIMARY KEY,
                crtif_no            TEXT,
                bld_nm              TEXT     NOT NULL,
                loc_addr            TEXT,
                rgn_sct_name        TEXT,
                full_addr           TEXT,
                jibun_addr          TEXT,
                bdmgt_sn            TEXT,
                purpose_kind_name   TEXT,
                bld_scale           TEXT,
                hshld_numb          INTEGER,
                exus_area_m2        REAL,
                real_area_m2        REAL,
                crtif_kind          TEXT,
                grd_name            TEXT,
                grade_code          TEXT,
                grade_rank          INTEGER,
                crtif_isu_dd        TEXT,
                app_rcpt_org_name   TEXT,
                ente_nm             TEXT,
                primary_energy_req  REAL,
                energy_reduce_rate  REAL,
                src_reg_dt          TEXT,
                fetched_at          TEXT,
                ingested_at         TEXT     NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT     NOT NULL DEFAULT (datetime('now'))
            )
        """)
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_beg_crtif_no     ON building_energy_grade (crtif_no)",
            "CREATE INDEX IF NOT EXISTS idx_beg_region       ON building_energy_grade (rgn_sct_name)",
            "CREATE INDEX IF NOT EXISTS idx_beg_grade_rank   ON building_energy_grade (grade_rank)",
            "CREATE INDEX IF NOT EXISTS idx_beg_isu_dd       ON building_energy_grade (crtif_isu_dd)",
            "CREATE INDEX IF NOT EXISTS idx_beg_purpose_kind ON building_energy_grade (purpose_kind_name, crtif_kind)",
            "CREATE INDEX IF NOT EXISTS idx_beg_bld_nm       ON building_energy_grade (bld_nm)",
        ):
            c.execute(stmt)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_beg_updated_at
            AFTER UPDATE ON building_energy_grade
            FOR EACH ROW
            BEGIN
                UPDATE building_energy_grade
                   SET updated_at = datetime('now')
                 WHERE rqid = OLD.rqid;
            END
        """)
        c.commit()

    # ---------- 적재 ----------
    UPSERT_SQL = """
        INSERT INTO building_energy_grade (
            rqid, crtif_no, bld_nm, loc_addr, rgn_sct_name, full_addr,
            purpose_kind_name, bld_scale, hshld_numb, exus_area_m2, real_area_m2,
            crtif_kind, grd_name, grade_code, grade_rank, crtif_isu_dd,
            app_rcpt_org_name, ente_nm, primary_energy_req, energy_reduce_rate,
            src_reg_dt, fetched_at
        ) VALUES (
            :rqid, :crtif_no, :bld_nm, :loc_addr, :rgn_sct_name, :full_addr,
            :purpose_kind_name, :bld_scale, :hshld_numb, :exus_area_m2, :real_area_m2,
            :crtif_kind, :grd_name, :grade_code, :grade_rank, :crtif_isu_dd,
            :app_rcpt_org_name, :ente_nm, :primary_energy_req, :energy_reduce_rate,
            :src_reg_dt, :fetched_at
        )
        ON CONFLICT(rqid) DO UPDATE SET
            crtif_no = excluded.crtif_no,
            bld_nm = excluded.bld_nm,
            loc_addr = excluded.loc_addr,
            full_addr = excluded.full_addr,
            grd_name = excluded.grd_name,
            grade_code = excluded.grade_code,
            grade_rank = excluded.grade_rank,
            crtif_isu_dd = excluded.crtif_isu_dd,
            primary_energy_req = excluded.primary_energy_req,
            energy_reduce_rate = excluded.energy_reduce_rate
    """

    def upsert_many(self, rows):
        """rows: dict 리스트. PK 충돌 시 일부 필드만 업데이트 (jibun/bdmgt는 보존)."""
        self.conn.executemany(self.UPSERT_SQL, rows)
        self.conn.commit()

    # ---------- 회수용 조회·업데이트 ----------
    def select_missing_jibun(self):
        return self.conn.execute("""
            SELECT rqid, full_addr, loc_addr
              FROM building_energy_grade
             WHERE (jibun_addr IS NULL OR jibun_addr = '')
             ORDER BY rqid
        """).fetchall()

    def select_missing_bdmgt(self):
        return self.conn.execute("""
            SELECT rqid, bld_nm, loc_addr, jibun_addr
              FROM building_energy_grade
             WHERE (bdmgt_sn IS NULL OR bdmgt_sn = '')
             ORDER BY rqid
        """).fetchall()

    def select_with_bdmgt_no_jibun(self):
        return self.conn.execute("""
            SELECT rqid, bld_nm, loc_addr, bdmgt_sn
              FROM building_energy_grade
             WHERE (jibun_addr IS NULL OR jibun_addr = '')
               AND bdmgt_sn IS NOT NULL AND length(bdmgt_sn) = 25
             ORDER BY rqid
        """).fetchall()

    def select_missing_jibun_by_name(self):
        """jibun 빈 행 중 건물명 있는 행 (단계 7 키워드검색 대상)."""
        return self.conn.execute("""
            SELECT rqid, bld_nm, loc_addr, full_addr
              FROM building_energy_grade
             WHERE (jibun_addr IS NULL OR jibun_addr = '')
               AND TRIM(COALESCE(bld_nm,'')) <> ''
             ORDER BY rqid
        """).fetchall()

    def select_code_to_loc(self):
        """이미 채워진 행에서 (시군구코드, 법정동코드) → (구, 동) 매핑 후보 수집."""
        return self.conn.execute("""
            SELECT jibun_addr, bdmgt_sn
              FROM building_energy_grade
             WHERE bdmgt_sn IS NOT NULL AND length(bdmgt_sn) = 25
               AND jibun_addr IS NOT NULL AND jibun_addr <> ''
        """).fetchall()

    def update_jibun(self, rqid, jibun):
        self.conn.execute(
            "UPDATE building_energy_grade SET jibun_addr=? WHERE rqid=? "
            "AND (jibun_addr IS NULL OR jibun_addr='')",
            (jibun, rqid),
        )

    def update_bdmgt(self, rqid, bdmgt):
        self.conn.execute(
            "UPDATE building_energy_grade SET bdmgt_sn=? WHERE rqid=? "
            "AND (bdmgt_sn IS NULL OR bdmgt_sn='')",
            (bdmgt, rqid),
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
              FROM building_energy_grade
        """).fetchone()
        return dict(row)


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
            jibun, status = self.derive(r["bdmgt_sn"], r["loc_addr"])
            stat[status] += 1
            if jibun is None:
                continue
            if status == "gu_mismatch":
                continue
            if status == "dong_mismatch" and not accept_dong_mismatch:
                continue
            self.db.update_jibun(r["rqid"], jibun)
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
        # 의미있는 검색을 위해 이름 최소 길이
        targets = [r for r in rows if r["bld_nm"] and len(r["bld_nm"].strip()) >= 3]
        print(f"  이름조회 대상: {len(targets)}건")

        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))

        stat = Counter()
        applied = 0
        for i, r in enumerate(targets, 1):
            loc_gu, loc_dong = self._parse(r["loc_addr"])
            if not loc_gu:
                stat["no_gu"] += 1
                continue
            juso_results = self.enricher.search_juso_by_name(r["bld_nm"].strip())
            time.sleep(JUSO_SLEEP)
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
                self.db.update_bdmgt(r["rqid"], pk)
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
    """jibun이 빈 행을 building_name(bld_nm)으로 카카오 키워드검색.

    카카오 주소검색(단계 3)·bdmgt 역추출(단계 5)·이름 bdmgt 매칭(단계 6)으로도
    못 채운 행을 장소(키워드)검색으로 회수한다. bdmgt 유무와 무관하게
    건물명만 있으면 대상으로 삼는다(정비사업·신축 등 bdmgt 없는 행이 다수).

    거짓양성 차단: 구(區) 일치 전제로 (a) 장소명이 건물명과 겹치거나
    (b) 결과 지번이 원문(full_addr) 지번과 일치할 때만 채택. 건물명에 박힌
    지번과 결과가 어긋나면 거부한다.
    """

    def __init__(self, db, enricher):
        self.db = db
        self.enricher = enricher

    @staticmethod
    def _gu(text):
        for t in (text or "").split():
            if t.endswith(("구", "군")):
                return t
        return ""

    @staticmethod
    def _core_tokens(name):
        name = re.sub(r"[\(\[].*?[\)\]]", " ", name)
        name = re.sub(r"[ⅠⅡⅢ()\[\]·,/]", " ", name)
        return [t for t in name.split() if len(t) >= 2]

    @staticmethod
    def _real_jibun(text):
        # '12-3' 하이픈형 또는 '123번지'만 진짜 지번으로 인정
        m = re.search(r"\d+-\d+|\d+(?=번지)", text or "")
        return m.group() if m else None

    def _pick(self, docs, gu, bname):
        toks = self._core_tokens(bname)
        best = None
        for d in docs:
            addr = d.get("address_name", "")
            place = d.get("place_name", "")
            if not (gu and gu in addr and "서울" in addr):
                continue
            if (any(t in place for t in toks)
                    or any(t in bname for t in self._core_tokens(place))):
                return addr, True
            if best is None:
                best = (addr, False)
        return best if best else (None, None)

    def run(self):
        rows = self.db.select_missing_jibun_by_name()
        print(f"  이름검색 대상: {len(rows)}건")

        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))

        stat = Counter()
        applied = 0
        for i, r in enumerate(rows, 1):
            bname = r["bld_nm"].strip()
            loc, full = r["loc_addr"] or "", r["full_addr"] or ""
            gu = self._gu(loc) or self._gu(full)

            result = confident = None
            for q in (bname, f"{gu} {bname}".strip()):
                docs = self.enricher.search_keyword(q)
                time.sleep(KAKAO_SLEEP)
                result, confident = self._pick(docs, gu, bname)
                if confident:
                    break

            name_jibun = self._real_jibun(bname)
            addr_jibun = self._real_jibun(full) or self._real_jibun(loc)
            tail = re.search(r"\d+-\d+|\d+$", result or "")
            tail = tail.group() if tail else None
            conflict = bool(name_jibun and tail and name_jibun != tail)
            jibun_ok = bool(tail and tail in (name_jibun, addr_jibun))

            if result and not conflict and (confident or jibun_ok):
                self.db.update_jibun(r["rqid"], result)
                applied += 1
                stat["match"] += 1
            elif result:
                stat["review"] += 1
            else:
                stat["miss"] += 1

            if i % BATCH_COMMIT_EVERY == 0:
                self.db.commit()
                print(f"    {i}/{len(rows)} | applied={applied}")
            if stop["v"]:
                break

        self.db.commit()
        print(f"  분포: {dict(stat)}")
        print(f"  반영: {applied}건")


# ============================================================
# 파이프라인 조립
# ============================================================
class EfficiencyPipeline:
    """전체 단계를 순서대로 실행한다."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.client = BeecApiClient(BEEC_KEY)
        self.enricher = AddressEnricher(KAKAO_KEY, JUSO_KEY)

    def run(
        self,
        fresh=False,
        limit_api_pages=0,
        skip_kakao=False,
        skip_juso=False,
        skip_recovery=False,
    ):
        # 0) 깨끗하게 다시 만들 거면 building_energy_grade 테이블만 비운다.
        # building_certifications.db는 gseed_certifications도 공유하므로 파일 자체는
        # 지우지 않는다.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if fresh and self.db_path.exists():
            print(f"[0] 기존 building_energy_grade 테이블 삭제")
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DROP TABLE IF EXISTS building_energy_grade")

        db = EfficiencyDB(self.db_path)
        db.create_schema()

        # 1) BEEC 수집 + 2) 정제·적재
        if not skip_kakao and not skip_juso:  # 기본은 수집부터
            print("[1] BEEC API 수집")
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
            db.upsert_many(transformed)

        # 3) 카카오 jibun_addr
        if not skip_kakao:
            print("[3] 카카오 주소검색 → jibun_addr")
            self._fill_jibun(db)

        # 4) Juso bdmgt_sn
        if not skip_juso:
            print("[4] Juso 도로명주소 → bdmgt_sn")
            self._fill_bdmgt(db)

        # 5) 회수: bdmgt → jibun 역추출
        if not skip_recovery:
            print("[5] 회수: bdmgt_sn → jibun_addr 역추출")
            JibunDeriver(db).run()

            # 6) 회수: 건물명+구 매칭
            print("[6] 회수: 건물명 + 구 필터로 bdmgt_sn 단일+동일치 매칭")
            NameLookup(db, self.enricher).run()

            # 7) 회수: 건물명 키워드검색 → jibun
            print("[7] 회수: 건물명 키워드검색 → jibun_addr")
            NameJibunLookup(db, self.enricher).run()

        # 최종 통계
        s = db.stats()
        print("\n=== 최종 상태 ===")
        print(f"  total      : {s['total']:,}")
        print(f"  no_jibun   : {s['no_jibun']:,}")
        print(f"  no_bdmgt   : {s['no_bdmgt']:,}")
        db.close()

    # ---- 단계 3 본체: 카카오 호출 루프 ----
    def _fill_jibun(self, db):
        rows = db.select_missing_jibun()
        print(f"  카카오 대상: {len(rows)}건")
        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))
        ok = miss = 0
        for i, r in enumerate(rows, 1):
            keyword = r["full_addr"] or r["loc_addr"]
            if not keyword:
                miss += 1
                continue
            jibun = self.enricher.fetch_jibun_addr(keyword)
            time.sleep(KAKAO_SLEEP)
            if jibun:
                db.update_jibun(r["rqid"], jibun)
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

    # ---- 단계 4 본체: Juso 호출 루프 ----
    def _fill_bdmgt(self, db):
        rows = db.select_missing_bdmgt()
        print(f"  Juso 대상: {len(rows)}건")
        stop = {"v": False}
        signal.signal(signal.SIGINT, lambda *_: stop.update(v=True) or print("\n중단 요청"))
        ok = miss = 0
        for i, r in enumerate(rows, 1):
            # 1순위 jibun_addr, 2순위 loc_addr (전자가 정확)
            keyword = r["jibun_addr"] or r["loc_addr"]
            if not keyword:
                miss += 1
                continue
            pk = self.enricher.fetch_bdmgt_sn(keyword)
            time.sleep(JUSO_SLEEP)
            if pk:
                db.update_bdmgt(r["rqid"], pk)
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
    parser = argparse.ArgumentParser(description="efficiency.db 전체 파이프라인 빌드")
    parser.add_argument("--fresh", action="store_true",
                        help="기존 DB 삭제 후 처음부터 빌드")
    parser.add_argument("--limit-api", type=int, default=0,
                        help="BEEC API 페이지 수 제한 (개발용, 0=전체)")
    parser.add_argument("--skip-kakao", action="store_true",
                        help="3단계(카카오 jibun) 건너뜀")
    parser.add_argument("--skip-juso", action="store_true",
                        help="4단계(Juso bdmgt) 건너뜀")
    parser.add_argument("--skip-recovery", action="store_true",
                        help="5·6단계(회수) 건너뜀")
    args = parser.parse_args()

    # 키 누락은 즉시 알려준다
    if not BEEC_KEY:
        print("[경고] BEEC_API_KEY가 없습니다. 1·2단계는 실패합니다.", file=sys.stderr)
    if not KAKAO_KEY and not args.skip_kakao:
        print("[경고] KAKAO_API_KEY가 없습니다. 3단계는 건너뛰는 것을 권장합니다.", file=sys.stderr)
    if not JUSO_KEY and not (args.skip_juso and args.skip_recovery):
        print("[경고] JUSO_API_KEY가 없습니다. 4·6단계는 건너뛰는 것을 권장합니다.", file=sys.stderr)

    pipeline = EfficiencyPipeline()
    pipeline.run(
        fresh=args.fresh,
        limit_api_pages=args.limit_api,
        skip_kakao=args.skip_kakao,
        skip_juso=args.skip_juso,
        skip_recovery=args.skip_recovery,
    )


if __name__ == "__main__":
    main()
