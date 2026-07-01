"""build_master_db.py — Single unified builder for complex_unified.db.

서울시 단지정보(Excel) + KB 부동산(API)을 직접 호출해 다음 DB 하나만 생성한다:

  ../DB/complex_unified.db
    ├── seoul_complex_raw    (Excel → Seoul 단지 + Kakao geocoding)
    ├── kb_complex_raw       (KB 단지 상세 + Kakao fallback)
    ├── kb_investment_raw    (KB 시세 multi-period merged)
    ├── apartment_complex       (매칭 결과 통합 마스터)
    └── complex_match_log    (매칭 의사결정 감사로그)

증분 캐싱: 토글이 False(기본)면 DB에 이미 있는 raw 테이블은 재호출하지 않고
매칭만 다시 돌린다(~10초). True로 바꾸면 해당 raw 테이블만 새로 빌드.

  REBUILD_SEOUL     — Excel 재읽기 + Kakao geocoding 새로 (~3분)
  REBUILD_KB_PRICE  — KB 시세 API 전체 호출 (~10~15분)
  REBUILD_KB_DETAIL — KB 단지 상세 API 전체 호출 (~15분, Kakao fallback 포함)
"""

# ===========================================================================
# Section 0: Imports / config
# ===========================================================================
import importlib.util
import subprocess
import sys

_REQUIRED_PIP = [("rapidfuzz", "rapidfuzz"),
                 ("python-dotenv", "dotenv"),
                 ("openpyxl", "openpyxl")]
_missing = [pip_name for pip_name, mod in _REQUIRED_PIP
            if importlib.util.find_spec(mod) is None]
if _missing:
    print(f"설치 필요: {_missing}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *_missing])

import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz

# ---- 경로 (스크립트 위치 기준 절대경로) ----
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = str(Path(SCRIPT_DIR).resolve().parent.parent.parent)
DB_DIR       = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "DB"))
RAW_DATA_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "raw_data"))
EXCEL_PATH   = os.path.join(RAW_DATA_DIR, "20260508_단지_기본정보.xlsx")

# infra.gateway import를 위해 프로젝트 루트를 sys.path에 추가
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from infra.gateway import get_default as _get_gateway  # noqa: E402
from real_estate.database import schema  # noqa: E402

_GW = _get_gateway()
OUT_DB = os.path.join(DB_DIR, schema.DB_FILENAME)

# ---- 증분 빌드 토글 ----
REBUILD_SEOUL     = False
REBUILD_KB_PRICE  = False
REBUILD_KB_DETAIL = False

# ---- 매칭 파라미터 ----
MATCH_RADIUS_M = 200
WEIGHTS = {
    "distance":   0.50,
    "address":    0.20,
    "name":       0.15,
    "household":  0.10,
    "completion": 0.05,
}
# (최대거리_m, 최소점수, method, needs_review)
THRESHOLDS = [
    (30,  0.90, "coord_strong",    0),
    (100, 0.80, "coord_address",   0),
    (150, 0.75, "coord_name_attr", 1),
]
EPSILON_REVIEW = 0.03  # 동점 차순위 가까우면 needs_review=1
KAKAO_WORKERS = 8  # 게이트웨이가 직렬화 → worker 늘려도 quota 보호

# ---- Kakao API ----
load_dotenv(Path(PROJECT_ROOT) / ".env")
KAKAO_API_KEY = os.getenv("KAKAO_API_KEY")
if not KAKAO_API_KEY:
    raise ValueError(".env 파일에 KAKAO_API_KEY가 설정되어 있지 않습니다.")

BUILT_AT = datetime.now().isoformat(timespec="seconds")


# ===========================================================================
# Section 1: 공용 헬퍼
# ===========================================================================
def table_exists(conn, name):
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


# ---- Kakao Local API ----
KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_HEADERS = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
_kakao_cache = {}


def _kakao_search(url, query):
    if not query or pd.isna(query):
        return None, None
    cache_key = (url, query)
    if cache_key in _kakao_cache:
        return _kakao_cache[cache_key]
    try:
        _GW.wait("kakao_local")
        resp = requests.get(url, headers=KAKAO_HEADERS,
                            params={"query": query}, timeout=5)
        resp.raise_for_status()
        docs = resp.json().get("documents", [])
        if docs:
            d = docs[0]
            result = (float(d["y"]), float(d["x"]))
        else:
            result = (None, None)
    except Exception as e:
        print(f"[kakao 검색 실패] {query} → {e}")
        result = (None, None)
    _kakao_cache[cache_key] = result
    return result


def geocode_address(address):
    return _kakao_search(KAKAO_ADDRESS_URL, address)


def geocode_keyword(name):
    return _kakao_search(KAKAO_KEYWORD_URL, name)


# ---- 주소/단지명 정규화 ----
ADDRESS_TAIL_RE = re.compile(r"(?:^|\s)\d+(?:-\d+)?(?=\s|$)")


def clean_address(address):
    """콤마로 여러 주소 묶인 경우 첫 번째만 사용."""
    if not address or pd.isna(address):
        return address
    if "," in address:
        return address.split(",")[0].strip()
    return address


def extract_address_only(address):
    """주소 뒤 건물명/단지명 제거, 지번 또는 도로명 번호까지 유지."""
    if not address or pd.isna(address):
        return None
    matches = list(ADDRESS_TAIL_RE.finditer(address))
    if not matches:
        return None
    return address[: matches[-1].end()].strip()


def clean_complex_name(name):
    """단지명 정규화: 괄호/대괄호/마침표/특수문자 제거. 매칭 시점에 .lower() 사용."""
    if not isinstance(name, str):
        return ""
    s = re.sub(r"\([^)]*\)", "", name)
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = s.replace(".", "")
    s = re.sub(r"[,·・~\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_ADDR_STOPWORDS = {"서울특별시", "서울시"}


def address_token_jaccard(a, b):
    if not a or not b:
        return 0.0
    ta = set(a.split()) - _ADDR_STOPWORDS
    tb = set(b.split()) - _ADDR_STOPWORDS
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def parse_completion_to_months(v):
    """YYYYMMDD / YYYYMM → year*12+month 정수."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 6:
        y, m = int(digits[:4]), int(digits[4:6])
        if 1900 < y < 2100 and 1 <= m <= 12:
            return y * 12 + m
    return None


def normalize_completion(v):
    """완공일을 'YYYY-MM' 문자열로."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    digits = re.sub(r"\D", "", str(v))
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return None


# ---- 거리 / 좌표 ----
EARTH_R = 6_371_000


def haversine_m(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def valid_coord(lat, lng):
    return pd.notna(lat) and pd.notna(lng) and lat != 0 and lng != 0


# ---- master 행 채울 때 KB/Seoul 값 선택 ----
def pick(first, second):
    for v in (first, second):
        if v is not None and pd.notna(v) and v != "" and v != 0:
            return v
    return None


def pick_max(*values):
    """결측/0 제외한 값 중 최댓값. 모두 무효면 None.
    세대수처럼 '부분 vs 전체 단지' 차이가 있을 때 전체(큰 값) 채택."""
    valid = [v for v in values if v is not None and pd.notna(v) and v != 0]
    return max(valid) if valid else None


# ---- 타입 변환 ----
def _to_int(v):
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    """KB API 응답 변환용. 0/'' 은 None."""
    if v in (None, "", "0", 0):
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (TypeError, ValueError):
        return None


def to_int_or_none(v):
    return int(v) if (v is not None and pd.notna(v)) else None


def to_text_or_none(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s else None


# ===========================================================================
# Section 2: 서울시 단지정보 빌드 (Excel → seoul_complex_raw)
# ===========================================================================
SEOUL_TABLE = "seoul_complex_raw"
SEOUL_RENAME = {
    "단지코드": "complex_code",
    "단지명": "complex_name",
    "법정동주소": "legal_address",
    "도로명주소": "road_address",
    "세대수": "household_count",
    "사용승인일": "준공년월일",
}


def _geocode_seoul_row(road_address, legal_address, complex_name=None):
    """road → road_stripped → legal → legal_stripped → 단지명 keyword."""
    candidates = []
    cleaned_road = clean_address(road_address)
    if cleaned_road and not pd.isna(cleaned_road):
        candidates.append((cleaned_road, "success_road"))
        rs = extract_address_only(cleaned_road)
        if rs and rs != cleaned_road:
            candidates.append((rs, "success_road_stripped"))
    cleaned_legal = clean_address(legal_address)
    if cleaned_legal and not pd.isna(cleaned_legal):
        candidates.append((cleaned_legal, "success_legal"))
        ls = extract_address_only(cleaned_legal)
        if ls and ls != cleaned_legal:
            candidates.append((ls, "success_legal_stripped"))
    for query, status in candidates:
        lat, lng = geocode_address(query)
        if lat is not None:
            return lat, lng, query, status
    if complex_name and not pd.isna(complex_name):
        lat, lng = geocode_keyword(complex_name)
        if lat is not None:
            return lat, lng, complex_name, "success_name"
    return None, None, None, "fail"


def build_seoul_complex_raw(conn):
    if not REBUILD_SEOUL and table_exists(conn, SEOUL_TABLE):
        print(f"[Section 2] {SEOUL_TABLE} 존재 → skip (REBUILD_SEOUL=False)")
        return pd.read_sql(f"SELECT * FROM {SEOUL_TABLE}", conn)

    print(f"[Section 2] {SEOUL_TABLE} 새로 빌드")
    if not os.path.exists(EXCEL_PATH):
        raise FileNotFoundError(f"입력 Excel 누락: {EXCEL_PATH}")

    df = pd.read_excel(EXCEL_PATH, header=1)
    print(f"  Excel 전체 행 수: {len(df)}")
    seoul_df = df[df["시도"] == "서울특별시"].copy()
    print(f"  서울특별시 단지 수: {len(seoul_df)}")
    seoul_df = seoul_df[list(SEOUL_RENAME.keys())].rename(columns=SEOUL_RENAME)

    from concurrent.futures import ThreadPoolExecutor

    total = len(seoul_df)
    inputs = [
        (row.road_address, row.legal_address, row.complex_name)
        for row in seoul_df.itertuples(index=False)
    ]
    lats, lngs, queries, statuses = [], [], [], []
    with ThreadPoolExecutor(max_workers=KAKAO_WORKERS) as ex:
        for i, (lat, lng, query, status) in enumerate(
            ex.map(lambda args: _geocode_seoul_row(*args), inputs), start=1
        ):
            lats.append(lat); lngs.append(lng)
            queries.append(query); statuses.append(status)
            if i % 100 == 0 or i == total:
                print(f"  geocoding 진행 {i}/{total}")

    seoul_df["latitude"]         = lats
    seoul_df["longitude"]        = lngs
    seoul_df["geocoding_query"]  = queries
    seoul_df["geocoding_status"] = statuses

    seoul_df.to_sql(SEOUL_TABLE, conn, if_exists="replace", index=False)
    conn.commit()
    print(f"  {SEOUL_TABLE} 저장 완료: {len(seoul_df)}건")
    return seoul_df


# ===========================================================================
# Section 3a: KB 시세 빌드 (KB API → kb_investment_raw)
# ===========================================================================
KB_INVESTMENT_TABLE = "kb_investment_raw"
KB_DATA_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://data.kbland.kr",
    "Referer": "https://data.kbland.kr/",
    "User-Agent": "Mozilla/5.0",
    "osType": "HUB",
}
KB_GU_URL    = "https://data-api.kbland.kr/bfmavm/map/siGunGuAreaNameList"
KB_DONG_URL  = "https://data-api.kbland.kr/bfmavm/map/stutDongAreaNameList"
KB_PRICE_URL = "https://api.kbland.kr/land-extra/price/v1/api/invstTblAptSearch"

INVESTMENT_PERIODS = [("12", "1년"), ("6", "6개월"), ("3", "3개월"),
                      ("1", "1개월"), ("01", "1주")]

INVESTMENT_STATIC_RENAME = {
    "데이터정보.I2010.단지명": "단지명",
    "데이터정보.I2020.공급면적": "공급면적_㎡",
    "데이터정보.I2020.공급면적평": "공급면적_평",
    "데이터정보.I2020.전용면적": "전용면적_㎡",
    "데이터정보.I2020.전용면적평": "전용면적_평",
    "데이터정보.I2020.주택형타입내용": "타입",
    "데이터정보.I2030.지수": "세대수",
    "데이터정보.I2040.지수": "연차",
    "데이터정보.I2050.지수": "가격지수_원/평",
    "데이터정보.I2050.공급면적당지수": "공급㎡당매매시세_만",
    "데이터정보.I2060.지수": "매매시세_억",
    "데이터정보.I2070.지수": "전세시세_억",
    "데이터정보.I2080.지수": "전세가율",
    "데이터정보.I2090.지수": "매매전세차_억",
    "지역명": "지역",
    "법정동코드": "동코드",
}
INVESTMENT_JEUNGGAM_RENAME = {
    "데이터정보.I2050.증감": "가격지수증감률",
    "데이터정보.I2050.공급면적당증감": "공급㎡당매매시세증감률",
    "데이터정보.I2060.증감": "매매시세증감률",
    "데이터정보.I2070.증감": "전세시세증감률",
    "데이터정보.I2080.증감": "전세가율증감률",
}


def _fetch_seoul_dongs():
    """서울 25개 구의 모든 법정동 목록."""
    _GW.wait("kb_land")
    resp = requests.get(KB_GU_URL, params={"법정동코드": "1100000000"},
                        headers=KB_DATA_HEADERS, timeout=20)
    resp.raise_for_status()
    gu_df = pd.json_normalize(resp.json()["dataBody"]["data"])

    all_dong = []
    for _, gu in gu_df.iterrows():
        _GW.wait("kb_land")
        resp = requests.get(KB_DONG_URL,
                            params={"법정동코드": gu["법정동코드"]},
                            headers=KB_DATA_HEADERS, timeout=20)
        resp.raise_for_status()
        tmp = pd.json_normalize(resp.json()["dataBody"]["data"])
        tmp["구명"] = gu["시군구명"]
        all_dong.append(tmp)
    return pd.concat(all_dong, ignore_index=True)


def build_kb_investment_raw(conn):
    if not REBUILD_KB_PRICE and table_exists(conn, KB_INVESTMENT_TABLE):
        print(f"[Section 3a] {KB_INVESTMENT_TABLE} 존재 → skip (REBUILD_KB_PRICE=False)")
        return pd.read_sql(f"SELECT * FROM {KB_INVESTMENT_TABLE}", conn)

    print(f"[Section 3a] {KB_INVESTMENT_TABLE} 새로 빌드 (10~15분 소요)")
    dong_df = _fetch_seoul_dongs()
    print(f"  동 목록: {len(dong_df)}")

    raw_by_period = {}
    for period_code, period_label in INVESTMENT_PERIODS:
        all_data = []
        for _, row in dong_df.iterrows():
            dong_code = row["법정동코드"]
            dong_name = row["법정동명"]
            try:
                _GW.wait("kb_land")
                resp = requests.get(
                    KB_PRICE_URL,
                    params={"기간코드": period_code, "지역리스트": dong_code},
                    headers=KB_DATA_HEADERS, timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
                if ("dataBody" in data and "data" in data["dataBody"]
                        and "데이터목록" in data["dataBody"]["data"]):
                    df = pd.json_normalize(data["dataBody"]["data"]["데이터목록"])
                elif "data" in data and isinstance(data["data"], list):
                    df = pd.json_normalize(data["data"])
                else:
                    continue
                if len(df) > 0:
                    df["동명"] = dong_name
                    df["동코드"] = dong_code
                    all_data.append(df)
            except Exception as e:
                print(f"  에러 [{period_label}] {dong_name}: {e}")
        period_df = pd.concat(all_data, ignore_index=True)
        raw_by_period[period_label] = period_df
        print(f"  [{period_label}] 완료: {period_df.shape}")

    # 컬럼 rename + 면적일련번호 중복 제거
    for label, df in raw_by_period.items():
        df = df.rename(columns={k: v for k, v in INVESTMENT_STATIC_RENAME.items() if k in df.columns})
        df = df.rename(columns={k: v for k, v in INVESTMENT_JEUNGGAM_RENAME.items() if k in df.columns})
        df = df.loc[:, ~df.columns.duplicated()]
        raw_by_period[label] = df
    for label, df in raw_by_period.items():
        if df["면적일련번호"].duplicated().any():
            raw_by_period[label] = df.drop_duplicates(subset=["면적일련번호"], keep="first")

    # 1주를 base로 + 다른 기간 증감만 merge
    base = raw_by_period["1주"].copy()
    jeunggam_cols = list(INVESTMENT_JEUNGGAM_RENAME.values())
    base = base.rename(columns={c: f"{c}_1주" for c in jeunggam_cols if c in base.columns})
    for label in ["1년", "6개월", "3개월", "1개월"]:
        df = raw_by_period[label]
        take = ["면적일련번호"] + [c for c in jeunggam_cols if c in df.columns]
        df_small = df[take].rename(columns={c: f"{c}_{label}" for c in jeunggam_cols})
        base = base.merge(df_small, on="면적일련번호", how="left")

    base.to_sql(KB_INVESTMENT_TABLE, conn, if_exists="replace", index=False)
    conn.commit()
    print(f"  {KB_INVESTMENT_TABLE} 저장 완료: {len(base)}건")
    return base


# ===========================================================================
# Section 3b: KB 단지 상세 빌드 (KB API + Kakao fallback → kb_complex_raw)
# ===========================================================================
KB_COMPLEX_TABLE = "kb_complex_raw"
KB_DETAIL_URL = "https://api.kbland.kr/land-complex/complex/main"
KB_DETAIL_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://kbland.kr",
    "Referer": "https://kbland.kr/",
    "User-Agent": "Mozilla/5.0",
    "osType": "HUB",
}

KB_COMPLEX_DDL = """
CREATE TABLE kb_complex_raw (
    단지기본일련번호 INTEGER PRIMARY KEY,
    단지명 TEXT,
    매물종별구분 TEXT,
    매물종별구분명 TEXT,
    wgs84위도 REAL,
    wgs84경도 REAL,
    법정동코드 TEXT,
    읍면동명 TEXT,
    구주소 TEXT,
    신주소 TEXT,
    도로기본주소 TEXT,
    도로명건물관리번호 TEXT,
    우편번호 TEXT,
    준공년월일 TEXT,
    준공년수 REAL,
    입주년월 TEXT,
    분양년월 TEXT,
    총동수 INTEGER,
    총세대수 INTEGER,
    일반세대수 INTEGER,
    조합세대수 INTEGER,
    임대세대수 INTEGER,
    최고층수 INTEGER,
    최저층수 INTEGER,
    총주차대수 INTEGER,
    세대당주차대수비율 REAL,
    난방방식구분명 TEXT,
    난방연료구분명 TEXT,
    현관구조 TEXT,
    시공사명 TEXT,
    시행업체명 TEXT,
    용적률내용 REAL,
    건폐율내용 REAL,
    대표공급면적 REAL,
    대표전용면적 REAL,
    대표계약면적 REAL,
    대지면적내용 REAL,
    건축면적내용 REAL,
    연면적내용 REAL,
    매매건수 INTEGER,
    전세건수 INTEGER,
    월세건수 INTEGER,
    재건축여부 TEXT,
    재개발여부 TEXT,
    내진설계여부 TEXT,
    도시형생활주택여부 TEXT,
    규제목록 TEXT,
    geocode_method TEXT,
    raw_json TEXT,
    fetched_at TEXT
)
"""

KB_INT_COLS = ["총동수", "총세대수", "일반세대수", "조합세대수", "임대세대수",
               "최고층수", "최저층수", "총주차대수",
               "매매건수", "전세건수", "월세건수"]
KB_FLOAT_COLS = ["wgs84위도", "wgs84경도", "준공년수", "세대당주차대수비율",
                 "용적률내용", "건폐율내용",
                 "대표공급면적", "대표전용면적", "대표계약면적",
                 "대지면적내용", "건축면적내용", "연면적내용"]
KB_TEXT_COLS = ["단지명", "매물종별구분", "매물종별구분명",
                "법정동코드", "읍면동명", "구주소", "신주소", "도로기본주소",
                "도로명건물관리번호", "우편번호",
                "준공년월일", "입주년월", "분양년월",
                "난방방식구분명", "난방연료구분명", "현관구조",
                "시공사명", "시행업체명",
                "재건축여부", "재개발여부", "내진설계여부", "도시형생활주택여부"]


def fetch_kb_complex_detail(complex_id):
    """KB 상세 API. (data_dict, error_str)."""
    try:
        _GW.wait("kb_land")
        resp = requests.get(KB_DETAIL_URL,
                            params={"단지기본일련번호": complex_id},
                            headers=KB_DETAIL_HEADERS, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        if body.get("dataHeader", {}).get("resultCode") != "10000":
            return None, body.get("dataHeader", {}).get("message", "unknown")
        data = body.get("dataBody", {}).get("data")
        if not data:
            return None, "empty data"
        return data, None
    except Exception as e:
        return None, str(e)


def _data_to_kb_row(data, geocode_method):
    row = {"단지기본일련번호": _to_int(data.get("단지기본일련번호"))}
    for c in KB_TEXT_COLS:
        row[c] = data.get(c)
    for c in KB_INT_COLS:
        row[c] = _to_int(data.get(c))
    for c in KB_FLOAT_COLS:
        row[c] = _to_float(data.get(c))
    reguls = data.get("regulList") or []
    row["규제목록"] = ",".join(r.get("규제명", "") for r in reguls if r.get("규제명")) or None
    row["geocode_method"] = geocode_method
    row["raw_json"] = json.dumps(data, ensure_ascii=False)
    row["fetched_at"] = pd.Timestamp.now().isoformat(timespec="seconds")
    return row


def build_kb_complex_raw(conn, inv_df):
    if not REBUILD_KB_DETAIL and table_exists(conn, KB_COMPLEX_TABLE):
        print(f"[Section 3b] {KB_COMPLEX_TABLE} 존재 → skip (REBUILD_KB_DETAIL=False)")
        return pd.read_sql(f"SELECT * FROM {KB_COMPLEX_TABLE}", conn)

    print(f"[Section 3b] {KB_COMPLEX_TABLE} 새로 빌드 (~15분 소요)")
    conn.execute(f"DROP TABLE IF EXISTS {KB_COMPLEX_TABLE}")
    conn.execute(KB_COMPLEX_DDL)
    conn.commit()

    ids = inv_df["단지기본일련번호"].dropna().unique()
    print(f"  대상 단지: {len(ids)}")

    insert_sql = None
    cols = None
    counts = {"kb_native": 0, "kakao_address": 0, "kakao_keyword": 0, "fail": 0}

    for i, cid in enumerate(ids, start=1):
        data, err = fetch_kb_complex_detail(int(cid))
        if not data:
            counts["fail"] += 1
            if i % 200 == 0 or i == len(ids):
                print(f"  진행 {i}/{len(ids)} {counts}")
            continue

        # 좌표 결정: KB native → Kakao 주소(신/구) → Kakao 키워드
        lat = _to_float(data.get("wgs84위도"))
        lng = _to_float(data.get("wgs84경도"))
        method = None
        if lat is not None and lng is not None:
            method = "kb_native"
        else:
            for addr_field, m in [("신주소", "kakao_address_road"),
                                  ("구주소", "kakao_address_jibun")]:
                addr = data.get(addr_field)
                if addr:
                    lat, lng = geocode_address(addr)
                    if lat is not None:
                        method = m
                        data["wgs84위도"] = lat
                        data["wgs84경도"] = lng
                        break
            if method is None and data.get("단지명"):
                lat, lng = geocode_keyword(data["단지명"])
                if lat is not None:
                    method = "kakao_keyword"
                    data["wgs84위도"] = lat
                    data["wgs84경도"] = lng

        if method is None:
            method = "none"
            counts["fail"] += 1
        elif method == "kb_native":
            counts["kb_native"] += 1
        elif method.startswith("kakao_address"):
            counts["kakao_address"] += 1
        elif method == "kakao_keyword":
            counts["kakao_keyword"] += 1

        row = _data_to_kb_row(data, method)
        if insert_sql is None:
            cols = list(row.keys())
            placeholders = ",".join(["?"] * len(cols))
            insert_sql = (f"INSERT OR REPLACE INTO {KB_COMPLEX_TABLE} "
                          f"({','.join(cols)}) VALUES ({placeholders})")
        conn.execute(insert_sql, [row[c] for c in cols])

        if i % 200 == 0 or i == len(ids):
            conn.commit()
            print(f"  진행 {i}/{len(ids)} {counts}")

    conn.commit()
    kb_df = pd.read_sql(f"SELECT * FROM {KB_COMPLEX_TABLE}", conn)
    print(f"  {KB_COMPLEX_TABLE} 저장 완료: {len(kb_df)}건 / {counts}")
    return kb_df


# ===========================================================================
# Section 4: 매칭 (후보 페어 → 스코어링 → greedy 결정 → 잔여)
# ===========================================================================
def generate_candidates(seoul_df, kb_df):
    """좌표 유효 필터 + bbox·haversine 후보 페어 생성 (순수, IO 없음).

    반환:
        pairs_df    {kb_id, seoul_code, distance_m} 후보 페어
        kb_idx      kb_df 를 단지기본일련번호로 인덱싱 (스코어링·master 빌드 재사용)
        seoul_idx   seoul_df 를 complex_code 로 인덱싱
        n_kb_valid  / n_seoul_valid   유효 좌표 행 수 (호출 측 로깅용)
    """
    # 좌표 유효 필터
    kb_valid = kb_df.dropna(subset=["wgs84위도", "wgs84경도"]).copy()
    kb_valid = kb_valid[(kb_valid["wgs84위도"] != 0) & (kb_valid["wgs84경도"] != 0)]
    seoul_valid = seoul_df.dropna(subset=["latitude", "longitude"]).copy()
    seoul_valid = seoul_valid[(seoul_valid["latitude"] != 0) & (seoul_valid["longitude"] != 0)]

    # 후보 페어 (bbox + haversine)
    kb_xy = kb_valid.rename(columns={
        "단지기본일련번호": "kb_id", "wgs84위도": "lat", "wgs84경도": "lng",
    })[["kb_id", "lat", "lng"]]
    s_lat  = seoul_valid["latitude"].to_numpy()
    s_lng  = seoul_valid["longitude"].to_numpy()
    s_code = seoul_valid["complex_code"].to_numpy()
    delta_lat = MATCH_RADIUS_M / 111_000

    pairs = []
    for r in kb_xy.itertuples(index=False):
        delta_lng = MATCH_RADIUS_M / (111_000 * math.cos(math.radians(r.lat)))
        mask = ((s_lat >= r.lat - delta_lat) & (s_lat <= r.lat + delta_lat) &
                (s_lng >= r.lng - delta_lng) & (s_lng <= r.lng + delta_lng))
        for j in mask.nonzero()[0]:
            d = haversine_m(r.lat, r.lng, s_lat[j], s_lng[j])
            if d <= MATCH_RADIUS_M:
                pairs.append({"kb_id": int(r.kb_id),
                              "seoul_code": str(s_code[j]),
                              "distance_m": d})
    pairs_df = pd.DataFrame(pairs)

    # 인덱스 (스코어링 + master 빌드에서 재사용)
    kb_idx    = kb_df.set_index("단지기본일련번호")
    seoul_idx = seoul_df.set_index("complex_code")

    return pairs_df, kb_idx, seoul_idx, len(kb_valid), len(seoul_valid)


def score_pairs(pairs_df, kb_idx, seoul_idx):
    """후보 페어마다 거리·주소·이름·세대수·준공 가중 점수 계산 (순수, IO 없음).

    반환: score 내림차순 정렬된 scored_df.
    """
    def _score_pair(kb_id, se_code, distance_m):
        kb = kb_idx.loc[kb_id]; se = seoul_idx.loc[se_code]
        s_dist = max(0.0, 1 - distance_m / MATCH_RADIUS_M)

        kb_addr = clean_address(kb.get("신주소") or kb.get("구주소"))
        se_addr = clean_address(se.get("road_address") or se.get("legal_address"))
        s_addr  = address_token_jaccard(kb_addr, se_addr) if (kb_addr or se_addr) else None

        kb_name = clean_complex_name(kb.get("단지명")).lower()
        se_name = clean_complex_name(se.get("complex_name")).lower()
        s_name  = (fuzz.token_set_ratio(kb_name, se_name) / 100.0) if (kb_name and se_name) else None

        kb_h, se_h = kb.get("총세대수"), se.get("household_count")
        if pd.notna(kb_h) and pd.notna(se_h) and max(kb_h, se_h) > 0:
            s_h    = 1 - min(1.0, abs(kb_h - se_h) / max(kb_h, se_h))
            h_diff = int(abs(kb_h - se_h))
        else:
            s_h, h_diff = None, None

        kb_cm = parse_completion_to_months(kb.get("준공년월일"))
        se_cm = parse_completion_to_months(se.get("준공년월일"))
        if kb_cm and se_cm:
            s_c    = max(0.0, 1 - abs(kb_cm - se_cm) / 24.0)
            c_diff = int(abs(kb_cm - se_cm))
        else:
            s_c, c_diff = None, None

        feats = {"distance": s_dist, "address": s_addr, "name": s_name,
                 "household": s_h, "completion": s_c}
        total_w = sum(WEIGHTS[k] for k, v in feats.items() if v is not None)
        if total_w == 0:
            return 0.0, feats, h_diff, c_diff
        score = sum(WEIGHTS[k] * v for k, v in feats.items() if v is not None) / total_w
        return score, feats, h_diff, c_diff

    scored = []
    for r in pairs_df.itertuples(index=False):
        sc, feats, h_diff, c_diff = _score_pair(r.kb_id, r.seoul_code, r.distance_m)
        scored.append({
            "kb_id": r.kb_id, "seoul_code": r.seoul_code, "distance_m": r.distance_m,
            "score": sc,
            "feat_address": feats["address"], "feat_name": feats["name"],
            "feat_household": feats["household"], "feat_completion": feats["completion"],
            "household_diff": h_diff, "completion_diff_months": c_diff,
        })
    scored_df = (pd.DataFrame(scored)
                 .sort_values("score", ascending=False)
                 .reset_index(drop=True))
    return scored_df


def greedy_select(scored_df):
    """점수 내림차순으로 1:1 그리디 매칭 + 임계 분류 + 동점 review 플래그 (순수, IO 없음).

    반환: matched_df (kb_id·seoul_code 각각 1회만 사용).
    """
    def _classify(distance_m, score):
        for max_d, min_s, method, nr in THRESHOLDS:
            if distance_m <= max_d and score >= min_s:
                return method, nr
        return None, None

    kb_scores, se_scores = {}, {}
    for r in scored_df.itertuples(index=False):
        kb_scores.setdefault(r.kb_id, []).append(r.score)
        se_scores.setdefault(r.seoul_code, []).append(r.score)

    used_kb, used_se = set(), set()
    matches = []
    for r in scored_df.itertuples(index=False):
        if r.kb_id in used_kb or r.seoul_code in used_se:
            continue
        method, nr = _classify(r.distance_m, r.score)
        if method is None:
            continue
        next_kb = max([s for s in kb_scores[r.kb_id]      if s < r.score] or [0])
        next_se = max([s for s in se_scores[r.seoul_code] if s < r.score] or [0])
        if (r.score - max(next_kb, next_se)) < EPSILON_REVIEW:
            nr = 1
        matches.append({
            "kb_id": r.kb_id, "seoul_code": r.seoul_code,
            "distance_m": r.distance_m, "score": r.score,
            "method": method, "needs_review": nr,
            "feat_address": r.feat_address, "feat_name": r.feat_name,
            "feat_household": r.feat_household, "feat_completion": r.feat_completion,
            "household_diff": r.household_diff,
            "completion_diff_months": r.completion_diff_months,
        })
        used_kb.add(r.kb_id); used_se.add(r.seoul_code)
    return pd.DataFrame(matches)


def run_matching(seoul_df, kb_df):
    """후보생성 → 스코어링 → 그리디 선택 오케스트레이션.

    순수 함수 3개(generate_candidates/score_pairs/greedy_select)를 순서대로 호출하고
    진행 로그·잔여 계산만 여기서 한다. 반환·동작은 분해 전과 동일하다.
    """
    pairs_df, kb_idx, seoul_idx, n_kb_valid, n_seoul_valid = generate_candidates(
        seoul_df, kb_df)
    print(f"[Section 4] 유효 좌표 KB={n_kb_valid} / Seoul={n_seoul_valid}")
    print(f"  후보 페어: {len(pairs_df)}")

    scored_df = score_pairs(pairs_df, kb_idx, seoul_idx)
    print(f"  스코어링: {len(scored_df)} 페어")

    matched_df = greedy_select(scored_df)
    print(f"  매칭: {len(matched_df)}")
    if not matched_df.empty:
        print(f"  method: {dict(matched_df['method'].value_counts())}")
        print(f"  needs_review: {int(matched_df['needs_review'].sum())}")

    # 잔여
    matched_kb = set(matched_df["kb_id"]) if not matched_df.empty else set()
    matched_se = set(matched_df["seoul_code"]) if not matched_df.empty else set()
    kb_only_df    = kb_df[~kb_df["단지기본일련번호"].isin(matched_kb)].copy()
    seoul_only_df = seoul_df[~seoul_df["complex_code"].isin(matched_se)].copy()
    print(f"  kb_only={len(kb_only_df)} / seoul_only={len(seoul_only_df)}")

    return matched_df, kb_only_df, seoul_only_df, kb_idx, seoul_idx


# ===========================================================================
# Section 5: apartment_complex 빌드 + 저장
# ===========================================================================
COMPLEX_MASTER_DDL = """
CREATE TABLE apartment_complex (
    master_complex_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name      TEXT NOT NULL,
    road_address        TEXT,
    legal_address       TEXT,
    latitude            REAL NOT NULL,
    longitude           REAL NOT NULL,
    household_count     INTEGER,
    completion_date     TEXT,
    total_buildings     INTEGER,
    seoul_complex_code  TEXT UNIQUE,
    kb_complex_id       INTEGER UNIQUE,
    data_source         TEXT NOT NULL,
    built_at            TEXT NOT NULL
)
"""

MASTER_COLS = [
    "canonical_name", "road_address", "legal_address",
    "latitude", "longitude",
    "household_count", "completion_date", "total_buildings",
    "seoul_complex_code", "kb_complex_id", "data_source", "built_at",
]


def build_apartment_complex(conn, matched_df, kb_only_df, seoul_only_df, kb_idx, seoul_idx):
    rows = []

    # matched: canonical_name 서울시 우선, household 큰 값, 그 외 KB 우선
    for r in matched_df.itertuples(index=False):
        kb = kb_idx.loc[r.kb_id]; se = seoul_idx.loc[r.seoul_code]
        lat = pick(kb.get("wgs84위도"), se.get("latitude"))
        lng = pick(kb.get("wgs84경도"), se.get("longitude"))
        if not valid_coord(lat, lng):
            continue
        rows.append({
            "canonical_name":     pick(se.get("complex_name"), kb.get("단지명")),
            "road_address":       pick(kb.get("신주소"), se.get("road_address")),
            "legal_address":      pick(kb.get("구주소"), se.get("legal_address")),
            "latitude":           float(lat),
            "longitude":          float(lng),
            "household_count":    pick_max(kb.get("총세대수"), se.get("household_count")),
            "completion_date":    pick(normalize_completion(kb.get("준공년월일")),
                                       normalize_completion(se.get("준공년월일"))),
            "total_buildings":    kb.get("총동수") if pd.notna(kb.get("총동수")) else None,
            "seoul_complex_code": r.seoul_code,
            "kb_complex_id":      int(r.kb_id),
            "data_source":        "matched",
            "built_at":           BUILT_AT,
        })

    for _, se in seoul_only_df.iterrows():
        lat, lng = se.get("latitude"), se.get("longitude")
        if not valid_coord(lat, lng):
            continue
        rows.append({
            "canonical_name":     se.get("complex_name"),
            "road_address":       se.get("road_address"),
            "legal_address":      se.get("legal_address"),
            "latitude":           float(lat),
            "longitude":          float(lng),
            "household_count":    se.get("household_count"),
            "completion_date":    normalize_completion(se.get("준공년월일")),
            "total_buildings":    None,
            "seoul_complex_code": se.get("complex_code"),
            "kb_complex_id":      None,
            "data_source":        "seoul_only",
            "built_at":           BUILT_AT,
        })

    for _, kb in kb_only_df.iterrows():
        lat, lng = kb.get("wgs84위도"), kb.get("wgs84경도")
        if not valid_coord(lat, lng):
            continue
        rows.append({
            "canonical_name":     kb.get("단지명"),
            "road_address":       kb.get("신주소"),
            "legal_address":      kb.get("구주소"),
            "latitude":           float(lat),
            "longitude":          float(lng),
            "household_count":    kb.get("총세대수"),
            "completion_date":    normalize_completion(kb.get("준공년월일")),
            "total_buildings":    kb.get("총동수") if pd.notna(kb.get("총동수")) else None,
            "seoul_complex_code": None,
            "kb_complex_id":      int(kb["단지기본일련번호"]),
            "data_source":        "kb_only",
            "built_at":           BUILT_AT,
        })

    master_df = pd.DataFrame(rows)
    print(f"[Section 5] apartment_complex 행: {len(master_df)}")
    print(f"  data_source: {dict(master_df['data_source'].value_counts())}")

    rows_for_insert = []
    for _, r in master_df.iterrows():
        rows_for_insert.append((
            to_text_or_none(r["canonical_name"]),
            to_text_or_none(r["road_address"]),
            to_text_or_none(r["legal_address"]),
            float(r["latitude"]),
            float(r["longitude"]),
            to_int_or_none(r["household_count"]),
            to_text_or_none(r["completion_date"]),
            to_int_or_none(r["total_buildings"]),
            to_text_or_none(r["seoul_complex_code"]),
            to_int_or_none(r["kb_complex_id"]),
            r["data_source"],
            r["built_at"],
        ))

    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS apartment_complex")
    cur.execute(COMPLEX_MASTER_DDL)
    placeholders = ",".join(["?"] * len(MASTER_COLS))
    cur.executemany(
        f"INSERT INTO apartment_complex ({','.join(MASTER_COLS)}) VALUES ({placeholders})",
        rows_for_insert,
    )
    conn.commit()
    master_with_id = pd.read_sql(
        "SELECT master_complex_id, seoul_complex_code, kb_complex_id, data_source "
        "FROM apartment_complex", conn,
    )
    return master_df, master_with_id


# ===========================================================================
# Section 6: complex_match_log 빌드 + 저장
# ===========================================================================
COMPLEX_MATCH_LOG_DDL = """
CREATE TABLE complex_match_log (
    log_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    master_complex_id       INTEGER REFERENCES apartment_complex(master_complex_id),
    seoul_complex_code      TEXT,
    kb_complex_id           INTEGER,
    match_score             REAL,
    match_method            TEXT NOT NULL,
    distance_meter          REAL,
    name_similarity         REAL,
    address_similarity      REAL,
    household_diff          INTEGER,
    completion_diff_months  INTEGER,
    needs_review            INTEGER NOT NULL DEFAULT 0,
    review_reason           TEXT,
    built_at                TEXT NOT NULL
)
"""


def build_complex_match_log(conn, matched_df, master_with_id):
    matched_lookup = (matched_df.set_index(["kb_id", "seoul_code"])
                      if not matched_df.empty else None)
    log_rows = []
    for _, m in master_with_id.iterrows():
        mcid    = int(m["master_complex_id"])
        kb_id   = m["kb_complex_id"]
        se_code = m["seoul_complex_code"]

        if (matched_lookup is not None and pd.notna(kb_id) and pd.notna(se_code)
                and (int(kb_id), se_code) in matched_lookup.index):
            mr = matched_lookup.loc[(int(kb_id), se_code)]
            log_rows.append({
                "master_complex_id":      mcid,
                "seoul_complex_code":     se_code,
                "kb_complex_id":          int(kb_id),
                "match_score":            float(mr["score"]),
                "match_method":           str(mr["method"]),
                "distance_meter":         float(mr["distance_m"]),
                "name_similarity":        float(mr["feat_name"])    if pd.notna(mr["feat_name"])    else None,
                "address_similarity":     float(mr["feat_address"]) if pd.notna(mr["feat_address"]) else None,
                "household_diff":         int(mr["household_diff"])       if pd.notna(mr["household_diff"]) else None,
                "completion_diff_months": int(mr["completion_diff_months"]) if pd.notna(mr["completion_diff_months"]) else None,
                "needs_review":           int(mr["needs_review"]),
                "review_reason":          None,
                "built_at":               BUILT_AT,
            })
        else:
            log_rows.append({
                "master_complex_id":      mcid,
                "seoul_complex_code":     se_code if pd.notna(se_code) else None,
                "kb_complex_id":          int(kb_id) if pd.notna(kb_id) else None,
                "match_score":            None,
                "match_method":           "unmatched",
                "distance_meter":         None,
                "name_similarity":        None,
                "address_similarity":     None,
                "household_diff":         None,
                "completion_diff_months": None,
                "needs_review":           0,
                "review_reason":          None,
                "built_at":               BUILT_AT,
            })

    log_df = pd.DataFrame(log_rows)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS complex_match_log")
    cur.execute(COMPLEX_MATCH_LOG_DDL)
    log_df.to_sql("complex_match_log", conn, if_exists="append", index=False)
    conn.commit()
    print(f"[Section 6] complex_match_log 저장: {len(log_df)}")
    return log_df


# ===========================================================================
# Section 7: 인덱스
# ===========================================================================
INDEX_STMTS = [
    "CREATE INDEX IF NOT EXISTS idx_master_lat        ON apartment_complex(latitude)",
    "CREATE INDEX IF NOT EXISTS idx_master_lng        ON apartment_complex(longitude)",
    "CREATE INDEX IF NOT EXISTS idx_master_seoul_code ON apartment_complex(seoul_complex_code)",
    "CREATE INDEX IF NOT EXISTS idx_master_kb_id      ON apartment_complex(kb_complex_id)",
    "CREATE INDEX IF NOT EXISTS idx_inv_kb_id         ON kb_investment_raw(단지기본일련번호)",
    "CREATE INDEX IF NOT EXISTS idx_log_master        ON complex_match_log(master_complex_id)",
    "CREATE INDEX IF NOT EXISTS idx_log_review        ON complex_match_log(needs_review) WHERE needs_review = 1",
]


def create_indexes(conn):
    cur = conn.cursor()
    for stmt in INDEX_STMTS:
        cur.execute(stmt)
    conn.commit()
    print("[Section 7] 인덱스 생성 완료")


# ===========================================================================
# Section 8: 검증
# ===========================================================================
def verify(conn):
    print("\n=== 테이블 행 수 ===")
    for tbl in ["seoul_complex_raw", "kb_complex_raw", "kb_investment_raw",
                "apartment_complex", "complex_match_log"]:
        n = pd.read_sql(f"SELECT COUNT(*) AS n FROM {tbl}", conn)["n"].iloc[0]
        print(f"  {tbl}: {n}")

    print("\n=== data_source 분포 ===")
    print(pd.read_sql(
        "SELECT data_source, COUNT(*) AS n FROM apartment_complex GROUP BY data_source",
        conn,
    ).to_string(index=False))

    print("\n=== match_method 분포 ===")
    print(pd.read_sql("""
        SELECT match_method, COUNT(*) AS n,
               ROUND(AVG(distance_meter), 2) AS avg_dist_m,
               ROUND(AVG(match_score), 3)    AS avg_score
          FROM complex_match_log GROUP BY match_method ORDER BY n DESC
    """, conn).to_string(index=False))

    print("\n=== JOIN sanity (시세 행 수 상위 5) ===")
    print(pd.read_sql("""
        SELECT m.canonical_name, COUNT(i.면적일련번호) AS price_rows
          FROM apartment_complex m
          JOIN kb_investment_raw i ON i.단지기본일련번호 = m.kb_complex_id
         WHERE m.kb_complex_id IS NOT NULL
         GROUP BY m.master_complex_id
         ORDER BY price_rows DESC LIMIT 5
    """, conn).to_string(index=False))


# ===========================================================================
# Main
# ===========================================================================
def main():
    os.makedirs(DB_DIR, exist_ok=True)
    print(f"OUT_DB     = {OUT_DB}")
    print(f"BUILT_AT   = {BUILT_AT}")
    print(f"REBUILD    = SEOUL:{REBUILD_SEOUL} / KB_PRICE:{REBUILD_KB_PRICE} / KB_DETAIL:{REBUILD_KB_DETAIL}")

    # timeout=60: 다른 빌더(efficiency 등)와 같은 DB를 공유할 때 락 대기 60초.
    # (WAL 모드 전환은 다른 connection 활성 시 즉시 실패하므로 사용 안 함)
    with sqlite3.connect(OUT_DB, timeout=60) as conn:
        seoul_df = build_seoul_complex_raw(conn)
        inv_df   = build_kb_investment_raw(conn)
        kb_df    = build_kb_complex_raw(conn, inv_df)

        matched_df, kb_only_df, seoul_only_df, kb_idx, seoul_idx = run_matching(seoul_df, kb_df)
        master_df, master_with_id = build_apartment_complex(
            conn, matched_df, kb_only_df, seoul_only_df, kb_idx, seoul_idx,
        )
        build_complex_match_log(conn, matched_df, master_with_id)
        create_indexes(conn)
        verify(conn)


if __name__ == "__main__":
    main()
