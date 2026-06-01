#!/usr/bin/env python
# coding: utf-8

# In[ ]:


# 필요한 패키지 설치 및 버전 확인
import importlib
import subprocess
import sys

REQUIRED = ["requests", "pandas", "openpyxl"]

# 누락된 패키지만 설치
missing = [pkg for pkg in REQUIRED if importlib.util.find_spec(pkg) is None]
if missing:
    print(f"설치 필요: {missing}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
else:
    print("모든 패키지가 이미 설치되어 있습니다.")

# 표준 라이브러리 포함 import 및 버전 확인
import os
import time

import pandas as pd
import requests
import openpyxl

print("\n=== 환경 확인 ===")
print(f"Python    : {sys.version.split()[0]}")
print(f"pandas    : {pd.__version__}")
print(f"requests  : {requests.__version__}")
print(f"openpyxl  : {openpyxl.__version__}")


# In[ ]:


headers = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://data.kbland.kr",
    "Referer": "https://data.kbland.kr/",
    "User-Agent": "Mozilla/5.0",
    "osType": "HUB",
}

url_gu = "https://data-api.kbland.kr/bfmavm/map/siGunGuAreaNameList"

params = {
    "법정동코드": "1100000000"  # 서울
}

gu_res = requests.get(url_gu, params=params, headers=headers)
gu_data = gu_res.json()

gu_df = pd.json_normalize(gu_data["dataBody"]["data"])
print("구 리스트:", gu_df.shape)
print(gu_df.head())


# In[5]:


url_dong = "https://data-api.kbland.kr/bfmavm/map/stutDongAreaNameList"

all_dong_df = []

for _, row in gu_df.iterrows():
    gu_code = row["법정동코드"]
    gu_name = row["시군구명"]  # '법정동명' → '시군구명'

    print(f"동 수집 중: {gu_name}")

    params = {
        "법정동코드": gu_code
    }

    res = requests.get(url_dong, params=params, headers=headers)
    dong_data = res.json()

    temp_df = pd.json_normalize(dong_data["dataBody"]["data"])  # dataBody 추가
    temp_df["구명"] = gu_name

    all_dong_df.append(temp_df)

    time.sleep(0.2)

dong_df = pd.concat(all_dong_df, ignore_index=True)

print("동 리스트:", dong_df.shape)
print(dong_df.head())


# In[ ]:


headers = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://data.kbland.kr",
    "Referer": "https://data.kbland.kr/",
    "User-Agent": "Mozilla/5.0",
    "osType": "HUB",
}

url_data = "https://api.kbland.kr/land-extra/price/v1/api/invstTblAptSearch"

PERIODS = [("12", "1년"), ("6", "6개월"), ("3", "3개월"), ("1", "1개월"), ("01", "1주")]

raw_by_period = {}

for period_code, period_label in PERIODS:
    all_data = []
    for _, row in dong_df.iterrows():
        dong_code = row["법정동코드"]
        dong_name = row["법정동명"]
        print(f"[{period_label}] 수집 중: {dong_name}")

        params = {"기간코드": period_code, "지역리스트": dong_code}
        try:
            res = requests.get(url_data, params=params, headers=headers, timeout=20)
            res.raise_for_status()
            data = res.json()

            if "dataBody" in data and "data" in data["dataBody"] and "데이터목록" in data["dataBody"]["data"]:
                df = pd.json_normalize(data["dataBody"]["data"]["데이터목록"])
            elif "data" in data and isinstance(data["data"], list):
                df = pd.json_normalize(data["data"])
            else:
                print(f"  경고: {dong_name} 응답 구조 불명 - {list(data.keys())}")
                continue

            if len(df) > 0:
                df["동명"] = dong_name
                df["동코드"] = dong_code
                all_data.append(df)

            time.sleep(0.3)

        except Exception as e:
            print(f"  에러: {dong_name} / {e}")

    period_df = pd.concat(all_data, ignore_index=True)
    raw_by_period[period_label] = period_df
    print(f"[{period_label}] 완료: {period_df.shape}")


# In[ ]:


STATIC_RENAME = {
    '데이터정보.I2010.단지명': '단지명',
    '데이터정보.I2020.공급면적': '공급면적_㎡',
    '데이터정보.I2020.공급면적평': '공급면적_평',
    '데이터정보.I2020.전용면적': '전용면적_㎡',
    '데이터정보.I2020.전용면적평': '전용면적_평',
    '데이터정보.I2020.주택형타입내용': '타입',
    '데이터정보.I2030.지수': '세대수',
    '데이터정보.I2040.지수': '연차',
    '데이터정보.I2050.지수': '가격지수_원/평',
    '데이터정보.I2050.공급면적당지수': '공급㎡당매매시세_만',
    '데이터정보.I2060.지수': '매매시세_억',
    '데이터정보.I2070.지수': '전세시세_억',
    '데이터정보.I2080.지수': '전세가율',
    '데이터정보.I2090.지수': '매매전세차_억',
    '지역명': '지역',
    '법정동코드': '동코드',
}

JEUNGGAM_RENAME = {
    '데이터정보.I2050.증감': '가격지수증감률',
    '데이터정보.I2050.공급면적당증감': '공급㎡당매매시세증감률',
    '데이터정보.I2060.증감': '매매시세증감률',
    '데이터정보.I2070.증감': '전세시세증감률',
    '데이터정보.I2080.증감': '전세가율증감률',
}

# 1) 각 기간 DataFrame 컬럼명 정리 + 동코드 중복 컬럼 제거
for label, df in raw_by_period.items():
    df = df.rename(columns={k: v for k, v in STATIC_RENAME.items() if k in df.columns})
    df = df.rename(columns={k: v for k, v in JEUNGGAM_RENAME.items() if k in df.columns})
    df = df.loc[:, ~df.columns.duplicated()]
    raw_by_period[label] = df

# 2) 면적일련번호 중복 체크 (동 경계 단지 대비)
for label, df in raw_by_period.items():
    dup = df["면적일련번호"].duplicated().sum()
    if dup:
        print(f"[{label}] 면적일련번호 중복 {dup}건 → 첫번째만 유지")
        raw_by_period[label] = df.drop_duplicates(subset=["면적일련번호"], keep="first")

# 3) 1주를 base로 — 스냅샷 컬럼 그대로 + 자기 증감엔 _1주 접미사
base = raw_by_period["1주"].copy()
jeunggam_cols = list(JEUNGGAM_RENAME.values())
base = base.rename(columns={c: f"{c}_1주" for c in jeunggam_cols if c in base.columns})

# 4) 다른 기간들은 [면적일련번호 + 증감 컬럼] 만 추출해서 left-merge
for label in ["1년", "6개월", "3개월", "1개월"]:
    df = raw_by_period[label]
    take = ["면적일련번호"] + [c for c in jeunggam_cols if c in df.columns]
    df_small = df[take].rename(columns={c: f"{c}_{label}" for c in jeunggam_cols})
    base = base.merge(df_small, on="면적일련번호", how="left")

# 5) 저장
base.to_csv("../raw_data/서울_투자데이터_통합.csv", index=False, encoding="utf-8-sig")

print("✅ 저장 완료:", base.shape)
print("컬럼:", base.columns.tolist())


# # DB 저장 + 카카오 좌표 추가
# 
# 지금까지 만든 `서울_투자데이터_통합.csv`를 SQLite DB(`kb_property.db`)로 저장합니다.
# 
# 두 개의 테이블을 만듭니다.
# 
# - **`raw_kb_investment`**: CSV 원본 그대로 (분석용 풀데이터)
# - **`complex_clean`**: 단지 단위로 정제 + 카카오 API로 위경도까지 붙인 핵심 테이블
# 
# 같은 노트북을 다시 돌려도 이미 좌표가 저장된 단지는 API를 다시 호출하지 않습니다.

# In[6]:


# 추가로 필요한 라이브러리 (기존 셀에서 받지 않은 것들만 설치)
import importlib

EXTRA_REQUIRED = ["dotenv", "tqdm"]
_pkg_install_map = {"dotenv": "python-dotenv", "tqdm": "tqdm"}

missing = [pkg for pkg in EXTRA_REQUIRED if importlib.util.find_spec(pkg) is None]
if missing:
    print(f"설치 필요: {missing}")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", *[_pkg_install_map[m] for m in missing]]
    )

import re
import sqlite3

from dotenv import load_dotenv
from tqdm.auto import tqdm

print("import 완료")


# ## 설정값
# 
# DB 파일과 테이블 이름, CSV 경로, 카카오 API 키를 한 곳에 모아둡니다.
# 
# `.env` 파일에 `KAKAO_REST_API_KEY`가 들어있어야 합니다.

# In[ ]:


CSV_PATH = "../raw_data/서울_투자데이터_통합.csv"
DB_PATH = "../DB/kb_property.db"
RAW_TABLE = "raw_kb_investment"
CLEAN_TABLE = "complex_clean"

load_dotenv()
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY")
if not KAKAO_REST_API_KEY:
    raise ValueError(".env 파일에 KAKAO_REST_API_KEY가 설정되어 있지 않습니다.")

print("설정 완료. KAKAO 키 앞 4자리:", KAKAO_REST_API_KEY[:4] + "****")


# ## CSV 불러오기
# 
# `서울_투자데이터_통합.csv`를 그대로 읽고, 면적일련번호가 정말 unique한지 확인합니다.
# 중복이 있으면 경고만 띄우고 계속 진행합니다(뒤 정제 단계에서 처리됨).

# In[8]:


df_raw = pd.read_csv(CSV_PATH, dtype={"동코드": str})
print("df_raw shape:", df_raw.shape)

dup_area = df_raw["면적일련번호"].duplicated().sum()
if dup_area > 0:
    print(f"경고: 면적일련번호 중복 {dup_area}건 발견")
else:
    print("면적일련번호 unique 확인 ✓")

print("단지기본일련번호 unique 개수:", df_raw["단지기본일련번호"].nunique())
df_raw.head(2)


# ## SQLite 연결 및 테이블 스키마 생성
# 
# `complex_clean`은 `단지기본일련번호`를 PRIMARY KEY로 두어 같은 단지를 두 번 저장하지 않게 합니다.
# `raw_kb_investment`는 컬럼이 많고 자주 갈아엎을 거라서, 다음 셀에서 `to_sql(if_exists="replace")`로 한 번에 만듭니다.

# In[ ]:


conn = sqlite3.connect(DB_PATH)

conn.execute(f"""
CREATE TABLE IF NOT EXISTS {CLEAN_TABLE} (
    단지기본일련번호 INTEGER PRIMARY KEY,
    단지명 TEXT,
    지역 TEXT,
    동코드 TEXT,
    세대수합산 INTEGER,
    geocode_status TEXT,
    latitude REAL,
    longitude REAL,
    geocode_method TEXT
)
""")
conn.commit()

# 기존 테이블에는 geocode_method 컬럼이 없을 수 있으므로 안전하게 추가
try:
    conn.execute(f"ALTER TABLE {CLEAN_TABLE} ADD COLUMN geocode_method TEXT")
    conn.commit()
    print(f"{CLEAN_TABLE}.geocode_method 컬럼 추가")
except sqlite3.OperationalError:
    pass  # 이미 컬럼이 있으면 무시

print(f"{CLEAN_TABLE} 테이블 준비 완료 (없으면 생성)")


# ## 원본 데이터 저장 — `raw_kb_investment`
# 
# CSV 전체를 그대로 SQLite에 떨궈둡니다. 매번 갈아엎습니다(`if_exists="replace"`).

# In[10]:


df_raw.to_sql(RAW_TABLE, conn, if_exists="replace", index=False)
conn.commit()

raw_count = pd.read_sql(f"SELECT COUNT(*) AS cnt FROM {RAW_TABLE}", conn)["cnt"].iloc[0]
print(f"{RAW_TABLE} 저장 완료: {raw_count}건")


# ## 단지 단위 정제 — `complex_clean` 만들기
# 
# 1. 완전히 동일한 행 제거
# 2. 면적일련번호 중복 제거 (혹시 모를 동 경계 단지 대비)
# 3. `단지기본일련번호`로 groupby → 단지 1개 = 행 1개로 압축
#    - 단지명/지역/동코드/동명은 그룹 내 첫 번째 값
#    - 세대수합산은 그룹 내 세대수 합
# 
# (동명은 카카오 검색 시 결과 선택용으로만 쓰고, DB에는 저장하지 않습니다.)

# In[11]:


df_dedup = df_raw.drop_duplicates()
df_dedup = df_dedup.drop_duplicates(subset=["면적일련번호"], keep="first")

# 세대수가 문자열로 들어와도 합산이 되도록 안전 변환
df_dedup["세대수"] = pd.to_numeric(df_dedup["세대수"], errors="coerce").fillna(0).astype(int)

complex_df = (
    df_dedup.groupby("단지기본일련번호", as_index=False)
    .agg(
        단지명=("단지명", "first"),
        지역=("지역", "first"),
        동코드=("동코드", "first"),
        동명=("동명", "first"),
        세대수합산=("세대수", "sum"),
    )
)
print("단지 수:", len(complex_df))
complex_df.head()


# ## 카카오 키워드 검색 함수
# 
# 카카오 Local API의 **키워드 검색**(`/v2/local/search/keyword.json`)만 사용합니다.
# 
# 검색 쿼리는 다음 순서로 시도하고, 어디서든 결과가 나오면 멈춥니다.
# 
# 1. `"서울특별시 {지역} {단지명}"`
# 2. `"{지역} {단지명}"`
# 3. `"{지역} {정제된 단지명}"`  (괄호/특수문자 제거)
# 
# 여러 결과가 나오면 다음 기준으로 하나를 고릅니다.
# 
# - 주소(`address_name` 또는 `road_address_name`)에 **구 이름과 동 이름이 모두 포함** → `success`
# - **구 이름만 포함** → `success`
# - 둘 다 아니면 첫 번째 결과 사용 → `ambiguous`
# - 결과 자체가 없으면 → `failed` (좌표 NULL)
# 
# 같은 쿼리를 두 번 안 보내도록 `_kakao_cache`에 결과를 보관합니다.

# In[12]:


KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
HEADERS = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}

_kakao_cache = {}


def clean_complex_name(name):
    """단지명 정제: 괄호+내용/대괄호+내용/마침표/일부 특수문자 제거 후 공백 정리."""
    if not isinstance(name, str):
        return ""
    s = re.sub(r"\([^)]*\)", "", name)
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = s.replace(".", "")
    s = re.sub(r"[,·・~\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _kakao_keyword(query):
    """카카오 keyword search 호출. (documents, error) 반환. 캐시 + sleep 포함."""
    if not query:
        return [], "empty"
    if query in _kakao_cache:
        return _kakao_cache[query]
    try:
        resp = requests.get(
            KAKAO_KEYWORD_URL,
            headers=HEADERS,
            params={"query": query, "size": 15},
            timeout=5,
        )
        resp.raise_for_status()
        docs = resp.json().get("documents", []) or []
        result = (docs, None)
    except Exception as e:
        result = ([], str(e))
    _kakao_cache[query] = result
    time.sleep(0.2)
    return result


def pick_best_document(docs, gu_name, dong_name):
    """후보 중 구/동 매칭 기준으로 하나 선택. match_level: gu_dong | gu | first."""
    if not docs:
        return None, None

    def addr_of(d):
        return (d.get("address_name") or "") + " " + (d.get("road_address_name") or "")

    if gu_name and dong_name:
        for d in docs:
            a = addr_of(d)
            if gu_name in a and dong_name in a:
                return d, "gu_dong"
    if gu_name:
        for d in docs:
            if gu_name in addr_of(d):
                return d, "gu"
    return docs[0], "first"


def geocode_complex(complex_name, gu_name, dong_name):
    """3단계 쿼리 시도. 성공 시 (lat, lng, 'success'|'ambiguous'), 실패 시 (None, None, 'failed')."""
    cleaned = clean_complex_name(complex_name)
    raw_candidates = [
        f"서울특별시 {gu_name} {complex_name}".strip(),
        f"{gu_name} {complex_name}".strip(),
        f"{gu_name} {cleaned}".strip() if cleaned and cleaned != complex_name else None,
    ]
    # None 제거 + 순서 유지 중복 제거
    candidates = list(dict.fromkeys([c for c in raw_candidates if c]))

    for q in candidates:
        docs, err = _kakao_keyword(q)
        if err or not docs:
            continue
        doc, level = pick_best_document(docs, gu_name, dong_name)
        if doc is None:
            continue
        try:
            lat = float(doc["y"])
            lng = float(doc["x"])
        except (KeyError, ValueError, TypeError):
            continue
        status = "success" if level in ("gu_dong", "gu") else "ambiguous"
        return lat, lng, status
    return None, None, "failed"


print("카카오 검색 함수 정의 완료")


# ## KB 단지 상세 API + 카카오 주소검색 (정확도 향상 파이프라인)
# 
# 키워드 검색은 동일/유사 단지명이 여러 단지에 있을 때 잘못된 좌표를 가져올 수 있습니다.
# KB부동산 단지 상세 API에서 공식 주소(신주소/구주소)를 받아 카카오 **주소 검색** API
# (`/v2/local/search/address.json`) 로 좌표를 붙이면 더 정확합니다.
# 
# **파이프라인:**
# 
# 1. `GET https://api.kbland.kr/land-complex/complex/main?단지기본일련번호={id}` 호출
# 2. 응답에서 `신주소`(도로명) / `구주소`(지번) 추출 → `raw_kb_complex_detail` 테이블에 저장
# 3. 카카오 주소검색: **신주소 우선 → 구주소 fallback**
# 4. 둘 다 실패하면 기존 키워드 검색 (`geocode_complex`) 으로 fallback
# 
# KB 상세 API JSON 구조 (예: `?단지기본일련번호=152`):
# ```json
# {
#   "dataBody": {
#     "data": {
#       "단지기본일련번호": 152,
#       "단지명": "잠실전화국주택조합(220동)",
#       "구주소": "서울특별시 강남구 개포동 655-2",
#       "신주소": "서울특별시 강남구 언주로 103",
#       ...
#     }
#   }
# }
# ```

# In[ ]:


import json

KB_DETAIL_URL = "https://api.kbland.kr/land-complex/complex/main"
KB_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://kbland.kr",
    "Referer": "https://kbland.kr/",
    "User-Agent": "Mozilla/5.0",
    "osType": "HUB",
}
KB_DETAIL_TABLE = "raw_kb_complex_detail"

# 스키마가 크게 바뀌므로 한 번 비우고 새로 생성
conn.execute(f"DROP TABLE IF EXISTS {KB_DETAIL_TABLE}")
conn.execute(f"""
CREATE TABLE {KB_DETAIL_TABLE} (
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
    raw_json TEXT,
    fetched_at TEXT
)
""")
conn.commit()


def _to_float(v):
    """문자열/None/숫자 → float or None. 0/'' 은 None 으로 취급."""
    if v in (None, "", "0", 0):
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (TypeError, ValueError):
        return None


def _to_int(v):
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def fetch_kb_complex_detail(complex_id):
    """KB 단지 상세 API 호출. (data_dict, error_str). sleep 포함."""
    try:
        resp = requests.get(
            KB_DETAIL_URL,
            params={"단지기본일련번호": complex_id},
            headers=KB_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("dataHeader", {}).get("resultCode") != "10000":
            return None, body.get("dataHeader", {}).get("message", "unknown")
        data = body.get("dataBody", {}).get("data")
        if not data:
            return None, "empty data"
        time.sleep(0.2)
        return data, None
    except Exception as e:
        return None, str(e)


INT_COLS = ["총동수", "총세대수", "일반세대수", "조합세대수", "임대세대수",
            "최고층수", "최저층수", "총주차대수",
            "매매건수", "전세건수", "월세건수"]
FLOAT_COLS = ["wgs84위도", "wgs84경도", "준공년수", "세대당주차대수비율",
              "용적률내용", "건폐율내용",
              "대표공급면적", "대표전용면적", "대표계약면적",
              "대지면적내용", "건축면적내용", "연면적내용"]
TEXT_COLS = ["단지명", "매물종별구분", "매물종별구분명",
             "법정동코드", "읍면동명", "구주소", "신주소", "도로기본주소",
             "도로명건물관리번호", "우편번호",
             "준공년월일", "입주년월", "분양년월",
             "난방방식구분명", "난방연료구분명", "현관구조",
             "시공사명", "시행업체명",
             "재건축여부", "재개발여부", "내진설계여부", "도시형생활주택여부"]


def save_kb_detail(conn, data):
    """KB 상세 응답을 raw_kb_complex_detail 테이블에 INSERT OR REPLACE."""
    row = {"단지기본일련번호": _to_int(data.get("단지기본일련번호"))}
    for c in TEXT_COLS:
        row[c] = data.get(c)
    for c in INT_COLS:
        row[c] = _to_int(data.get(c))
    for c in FLOAT_COLS:
        row[c] = _to_float(data.get(c))

    reguls = data.get("regulList") or []
    row["규제목록"] = ",".join(r.get("규제명", "") for r in reguls if r.get("규제명")) or None
    row["raw_json"] = json.dumps(data, ensure_ascii=False)
    row["fetched_at"] = pd.Timestamp.now().isoformat(timespec="seconds")

    cols = list(row.keys())
    placeholders = ",".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO {KB_DETAIL_TABLE} ({','.join(cols)}) "
        f"VALUES ({placeholders})",
        [row[c] for c in cols],
    )


print(f"{KB_DETAIL_TABLE} 테이블 재생성 + KB 상세 API 함수 준비 완료")


# In[ ]:


KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
_kakao_addr_cache = {}


def kakao_search_address(query):
    """카카오 주소검색 API. 성공 시 (lat, lng), 실패 시 (None, None)."""
    if not query or not query.strip():
        return None, None
    if query in _kakao_addr_cache:
        return _kakao_addr_cache[query]
    try:
        resp = requests.get(
            KAKAO_ADDRESS_URL,
            headers=HEADERS,  # 기존 셀에서 정의된 카카오 헤더 재사용
            params={"query": query},
            timeout=5,
        )
        resp.raise_for_status()
        docs = resp.json().get("documents", []) or []
        if not docs:
            result = (None, None)
        else:
            first = docs[0]
            result = (float(first["y"]), float(first["x"]))
    except Exception:
        result = (None, None)
    _kakao_addr_cache[query] = result
    time.sleep(0.2)
    return result


print("카카오 주소검색 함수 정의 완료")


# In[ ]:


def geocode_complex_v2(complex_id, complex_name, gu_name, dong_name, conn):
    """KB 상세 API → 카카오 주소검색 → 키워드 fallback 순으로 좌표 획득.

    반환: (lat, lng, status, method)
        status: 'success' | 'ambiguous' | 'failed'
        method:
          'kb_native'           — KB API 의 wgs84 좌표를 직접 사용 (1순위)
          'kakao_address_road'  — 신주소(도로명)로 카카오 주소검색
          'kakao_address_jibun' — 구주소(지번)로 카카오 주소검색
          'kakao_keyword'       — 키워드 검색 fallback
          'none'                — 모두 실패
    """
    # 1) KB 단지 상세 API
    data, err = fetch_kb_complex_detail(complex_id)
    if data:
        save_kb_detail(conn, data)

        # 1-a) KB 응답의 wgs84 좌표 직접 사용 (1순위)
        lat = _to_float(data.get("wgs84위도"))
        lng = _to_float(data.get("wgs84경도"))
        if lat is not None and lng is not None:
            return lat, lng, "success", "kb_native"

        # 1-b) 카카오 주소검색: 신주소(도로명) → 구주소(지번)
        for addr_field, method in [
            ("신주소", "kakao_address_road"),
            ("구주소", "kakao_address_jibun"),
        ]:
            addr = data.get(addr_field)
            if addr:
                lat, lng = kakao_search_address(addr)
                if lat is not None:
                    return lat, lng, "success", method

    # 2) 키워드 fallback (기존 geocode_complex)
    lat, lng, status = geocode_complex(complex_name, gu_name, dong_name)
    if lat is not None:
        return lat, lng, status, "kakao_keyword"
    return None, None, "failed", "none"


print("geocode_complex_v2 정의 완료 (KB 좌표 1순위)")


# ## 이미 좌표가 있는 단지는 재호출 안 하기
# 
# `complex_clean`에 이미 `latitude`가 있는 단지는 DB에서 그대로 가져와 재사용합니다.
# 좌표가 없는(`latitude IS NULL`) 단지나 새로 추가된 단지만 API를 호출합니다.
# 
# > 실패(`failed`) 단지는 좌표가 NULL이므로 다음 실행 때 자동으로 다시 시도됩니다.
# > 영구히 건너뛰고 싶으면 이 셀의 조건을 바꾸시면 됩니다.

# In[ ]:


# 재처리 모드 선택
# - "missing_only"        : 좌표가 NULL 인 단지만 새 파이프라인 실행
# - "failed_or_ambiguous" : geocode_status 가 failed/ambiguous + 좌표 NULL 인 단지 재처리
# - "all"                 : 3583개 전부 새 파이프라인 (KB API 3583회 호출, 약 12분)
REPROCESS_MODE = "all"

# 기존 DB 상태 로드 (단지기본일련번호 → (lat, lng, status))
existing_rows = {}
try:
    cur = conn.execute(
        f"SELECT 단지기본일련번호, latitude, longitude, geocode_status "
        f"FROM {CLEAN_TABLE}"
    )
    for cid, lat, lng, status in cur.fetchall():
        existing_rows[cid] = (lat, lng, status)
except sqlite3.OperationalError:
    pass
print(f"DB에 이미 저장된 단지: {len(existing_rows)}건")


def should_reprocess(cid, prev):
    if REPROCESS_MODE == "all":
        return True
    if prev is None:  # 처음 보는 단지
        return True
    lat, lng, status = prev
    if REPROCESS_MODE == "missing_only":
        return lat is None
    if REPROCESS_MODE == "failed_or_ambiguous":
        return status in ("failed", "ambiguous") or lat is None
    return True


target_count = sum(
    1 for row in complex_df.itertuples(index=False)
    if should_reprocess(row.단지기본일련번호, existing_rows.get(row.단지기본일련번호))
)
print(f"재처리 대상: {target_count}건 (REPROCESS_MODE={REPROCESS_MODE})")

lats, lngs, statuses, methods = [], [], [], []
for row in tqdm(complex_df.itertuples(index=False), total=len(complex_df)):
    cid = row.단지기본일련번호
    prev = existing_rows.get(cid)
    if not should_reprocess(cid, prev):
        lat, lng, status = prev
        method = "kept_existing"
    else:
        try:
            lat, lng, status, method = geocode_complex_v2(
                cid, row.단지명, row.지역, row.동명, conn
            )
        except Exception as e:
            print(f"[error] {cid} {row.단지명}: {e}")
            lat, lng, status, method = None, None, "failed", "none"
    lats.append(lat)
    lngs.append(lng)
    statuses.append(status)
    methods.append(method)

conn.commit()  # KB 상세 응답 저장 flush

complex_df["latitude"] = lats
complex_df["longitude"] = lngs
complex_df["geocode_status"] = statuses
complex_df["geocode_method"] = methods
complex_df.head()


# ## `complex_clean` 저장 (INSERT OR REPLACE)
# 
# `단지기본일련번호`가 PRIMARY KEY 라서 같은 단지가 들어오면 행을 덮어씁니다.
# 이렇게 하면 노트북을 여러 번 돌려도 중복이 안 생깁니다.

# In[ ]:


save_df = complex_df[[
    "단지기본일련번호", "단지명", "지역", "동코드",
    "세대수합산", "geocode_status", "latitude", "longitude", "geocode_method",
]]

conn.executemany(
    f"""INSERT OR REPLACE INTO {CLEAN_TABLE}
        (단지기본일련번호, 단지명, 지역, 동코드,
         세대수합산, geocode_status, latitude, longitude, geocode_method)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
    save_df.itertuples(index=False, name=None),
)
conn.commit()
print(f"{CLEAN_TABLE} 저장 완료: {len(save_df)}건")


# ## 결과 검증
# 
# - 전체 raw 건수
# - 정제된 단지 수
# - 좌표 검색 결과(`geocode_status`) 분포 — success / ambiguous / failed
# - `complex_clean` 상위 5건 미리보기
# 
# 마지막에 DB 연결도 닫습니다.

# In[ ]:


raw_count = pd.read_sql(f"SELECT COUNT(*) AS cnt FROM {RAW_TABLE}", conn)["cnt"].iloc[0]
clean_count = pd.read_sql(f"SELECT COUNT(*) AS cnt FROM {CLEAN_TABLE}", conn)["cnt"].iloc[0]
kb_detail_count = pd.read_sql(
    f"SELECT COUNT(*) AS cnt FROM {KB_DETAIL_TABLE}", conn
)["cnt"].iloc[0]
print(f"{RAW_TABLE}: {raw_count}건")
print(f"{CLEAN_TABLE}: {clean_count}건")
print(f"{KB_DETAIL_TABLE}: {kb_detail_count}건")

status_dist = pd.read_sql(
    f"SELECT geocode_status, COUNT(*) AS cnt FROM {CLEAN_TABLE} "
    f"GROUP BY geocode_status ORDER BY cnt DESC",
    conn,
)
print("\ngeocode_status 분포:")
print(status_dist)

method_dist = pd.read_sql(
    f"SELECT geocode_method, COUNT(*) AS cnt FROM {CLEAN_TABLE} "
    f"GROUP BY geocode_method ORDER BY cnt DESC",
    conn,
)
print("\ngeocode_method 분포:")
print(method_dist)

# kb_native 좌표 샘플 — 카카오 지도에서 직접 검증할 수 있게 주소도 함께 표시
kb_native_sample = pd.read_sql(
    f"""
    SELECT c.단지기본일련번호, c.단지명, c.지역,
           c.latitude, c.longitude,
           d.신주소, d.구주소
    FROM {CLEAN_TABLE} c
    LEFT JOIN {KB_DETAIL_TABLE} d ON c.단지기본일련번호 = d.단지기본일련번호
    WHERE c.geocode_method = 'kb_native'
    ORDER BY RANDOM() LIMIT 10
    """,
    conn,
)
print("\nkb_native 좌표 샘플 (랜덤 10건):")
print(kb_native_sample)

# 카카오 fallback 으로 떨어진 단지 (KB 좌표가 비어있던 케이스)
fallback_sample = pd.read_sql(
    f"""
    SELECT 단지기본일련번호, 단지명, 지역,
           latitude, longitude, geocode_status, geocode_method
    FROM {CLEAN_TABLE}
    WHERE geocode_method != 'kb_native'
    LIMIT 20
    """,
    conn,
)
print("\nKB 좌표가 없어서 fallback 으로 떨어진 단지 (최대 20건):")
print(fallback_sample)

top5 = pd.read_sql(f"SELECT * FROM {CLEAN_TABLE} LIMIT 5", conn)

conn.close()
top5

