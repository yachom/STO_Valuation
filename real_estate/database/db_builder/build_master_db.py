#!/usr/bin/env python
# coding: utf-8

# # Master DB 통합 노트북 (`complex_unified.db`)
# 
# 서울시 단지정보(`complex_basic_info.db`)와 KB 부동산(`kb_property.db`) 두 소스를
# 하나의 master DB로 통합한다.
# 
# 산출물: `../DB/complex_unified.db`
# 
# - `seoul_complex_raw` — 서울시 원본 복사
# - `kb_complex_raw` — KB 단지 상세 원본 복사
# - `kb_investment_raw` — KB 시세 원본 복사
# - `complex_master` — 통합 단지 마스터
# - `complex_match_log` — 매칭 의사결정 감사로그
# 
# 매칭 우선순위: 좌표 거리 > 주소 유사도 > 단지명 유사도 > 세대수 차이 > 준공년월 차이.

# ## 1. 환경 설정
# 
# 필요 패키지 설치, 경로 및 매칭 파라미터 정의.

# In[1]:


import importlib.util
import subprocess
import sys

REQUIRED = ["rapidfuzz"]
missing = [p for p in REQUIRED if importlib.util.find_spec(p) is None]
if missing:
    print("설치 필요:", missing)
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])

import math
import os
import re
import sqlite3
from datetime import datetime

import pandas as pd
from rapidfuzz import fuzz

print("import 완료")


# In[2]:


# 경로
SEOUL_DB = "../DB/complex_basic_info.db"
KB_DB    = "../DB/kb_property.db"
OUT_DB   = "../DB/complex_unified.db"

# 매칭 파라미터
MATCH_RADIUS_M = 200

WEIGHTS = {
    "distance":   0.50,
    "address":    0.20,
    "name":       0.15,
    "household":  0.10,
    "completion": 0.05,
}

# (최대거리_m, 최소점수, method 명, needs_review)
THRESHOLDS = [
    (30,  0.90, "coord_strong",    0),
    (100, 0.80, "coord_address",   0),
    (150, 0.75, "coord_name_attr", 1),
]

EPSILON_REVIEW = 0.03  # 동점 차순위 가까우면 needs_review=1
BUILT_AT = datetime.now().isoformat(timespec="seconds")
print("BUILT_AT:", BUILT_AT)


# ## 2. 소스 DB 로드
# 
# 서울시 DB의 `apt_complex_basic`, KB DB의 `raw_kb_complex_detail`·`raw_kb_investment`을 읽는다.

# In[3]:


with sqlite3.connect(SEOUL_DB) as conn:
    seoul_df = pd.read_sql("SELECT * FROM apt_complex_basic", conn)
print("seoul_df:", seoul_df.shape)

with sqlite3.connect(KB_DB) as conn:
    kb_df  = pd.read_sql("SELECT * FROM raw_kb_complex_detail", conn)
    inv_df = pd.read_sql("SELECT * FROM raw_kb_investment", conn)
print("kb_df:", kb_df.shape)
print("inv_df:", inv_df.shape)


# ## 3. raw 테이블 적재
# 
# `complex_unified.db`에 소스 DataFrame 3개를 그대로 복사한다. 이름만 관습에 맞게 변경:
# - `apt_complex_basic` → `seoul_complex_raw`
# - `raw_kb_complex_detail` → `kb_complex_raw`
# - `raw_kb_investment` → `kb_investment_raw`

# In[4]:


os.makedirs(os.path.dirname(OUT_DB), exist_ok=True)

with sqlite3.connect(OUT_DB) as conn:
    seoul_df.to_sql("seoul_complex_raw", conn, if_exists="replace", index=False)
    kb_df.to_sql("kb_complex_raw",       conn, if_exists="replace", index=False)
    inv_df.to_sql("kb_investment_raw",   conn, if_exists="replace", index=False)
    conn.commit()

print("raw 적재 완료 →", OUT_DB)


# ## 4. 후보 페어 생성
# 
# KB 각 단지 좌표 기준 bbox로 1차 필터 후 haversine으로 정확 거리 채점.
# 200m 이내의 (KB, Seoul) 페어만 유지한다.

# In[5]:


EARTH_R = 6_371_000

def haversine_m(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * EARTH_R * math.asin(math.sqrt(a))

# 좌표 유효성 필터
kb_valid = kb_df.dropna(subset=["wgs84위도", "wgs84경도"]).copy()
kb_valid = kb_valid[(kb_valid["wgs84위도"] != 0) & (kb_valid["wgs84경도"] != 0)]
print("KB 유효 좌표:", len(kb_valid), "/", len(kb_df))

seoul_valid = seoul_df.dropna(subset=["latitude", "longitude"]).copy()
seoul_valid = seoul_valid[(seoul_valid["latitude"] != 0) & (seoul_valid["longitude"] != 0)]
print("Seoul 유효 좌표:", len(seoul_valid), "/", len(seoul_df))


# In[6]:


# 한글 컬럼명을 itertuples용으로 ASCII로 리네임
kb_xy = kb_valid.rename(columns={
    "단지기본일련번호": "kb_id",
    "wgs84위도":      "lat",
    "wgs84경도":      "lng",
})[["kb_id", "lat", "lng"]]

seoul_lat  = seoul_valid["latitude"].to_numpy()
seoul_lng  = seoul_valid["longitude"].to_numpy()
seoul_code = seoul_valid["complex_code"].to_numpy()

delta_lat = MATCH_RADIUS_M / 111_000
pairs = []
for r in kb_xy.itertuples(index=False):
    delta_lng = MATCH_RADIUS_M / (111_000 * math.cos(math.radians(r.lat)))
    mask = (
        (seoul_lat >= r.lat - delta_lat) & (seoul_lat <= r.lat + delta_lat) &
        (seoul_lng >= r.lng - delta_lng) & (seoul_lng <= r.lng + delta_lng)
    )
    for j in mask.nonzero()[0]:
        d = haversine_m(r.lat, r.lng, seoul_lat[j], seoul_lng[j])
        if d <= MATCH_RADIUS_M:
            pairs.append({
                "kb_id":      int(r.kb_id),
                "seoul_code": str(seoul_code[j]),
                "distance_m": d,
            })

pairs_df = pd.DataFrame(pairs)
print("후보 페어:", len(pairs_df))
if not pairs_df.empty:
    print(pairs_df["distance_m"].describe().round(1))


# ## 5. 페어 스코어링
# 
# 각 페어에 대해 distance/address/name/household/completion 5개 feature 점수를 계산하고
# 결측을 제외한 재정규화 가중합으로 종합 score를 만든다.

# In[7]:


def clean_name(name):
    """단지명 정규화: 괄호/대괄호/마침표/특수문자 제거 + lower."""
    if not isinstance(name, str):
        return ""
    s = re.sub(r"\([^)]*\)", "", name)
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = s.replace(".", "")
    s = re.sub(r"[,·・~\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()

def clean_address(addr):
    """주소 정규화: 콤마 앞부분만 + 공백 정리."""
    if not isinstance(addr, str):
        return ""
    if "," in addr:
        addr = addr.split(",")[0]
    return re.sub(r"\s+", " ", addr).strip()

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
    """준공년월일 값을 (year*12 + month) 정수로. 파싱 불가시 None."""
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

print("정규화 함수 로드 완료")


# In[8]:


kb_idx    = kb_df.set_index("단지기본일련번호")
seoul_idx = seoul_df.set_index("complex_code")

def score_pair(kb_id, se_code, distance_m):
    kb = kb_idx.loc[kb_id]
    se = seoul_idx.loc[se_code]

    s_dist = max(0.0, 1 - distance_m / MATCH_RADIUS_M)

    kb_addr = clean_address(kb.get("신주소") or kb.get("구주소"))
    se_addr = clean_address(se.get("road_address") or se.get("legal_address"))
    s_addr = address_token_jaccard(kb_addr, se_addr) if (kb_addr or se_addr) else None

    kb_name = clean_name(kb.get("단지명"))
    se_name = clean_name(se.get("complex_name"))
    s_name = (fuzz.token_set_ratio(kb_name, se_name) / 100.0) if (kb_name and se_name) else None

    kb_h = kb.get("총세대수")
    se_h = se.get("household_count")
    if pd.notna(kb_h) and pd.notna(se_h) and max(kb_h, se_h) > 0:
        s_h = 1 - min(1.0, abs(kb_h - se_h) / max(kb_h, se_h))
        h_diff = int(abs(kb_h - se_h))
    else:
        s_h, h_diff = None, None

    kb_cm = parse_completion_to_months(kb.get("준공년월일"))
    se_cm = parse_completion_to_months(se.get("준공년월일"))
    if kb_cm and se_cm:
        s_c = max(0.0, 1 - abs(kb_cm - se_cm) / 24.0)
        c_diff = int(abs(kb_cm - se_cm))
    else:
        s_c, c_diff = None, None

    feats = {
        "distance":   s_dist,
        "address":    s_addr,
        "name":       s_name,
        "household":  s_h,
        "completion": s_c,
    }
    total_w = sum(WEIGHTS[k] for k, v in feats.items() if v is not None)
    if total_w == 0:
        return 0.0, feats, h_diff, c_diff
    score = sum(WEIGHTS[k] * v for k, v in feats.items() if v is not None) / total_w
    return score, feats, h_diff, c_diff

scored = []
for r in pairs_df.itertuples(index=False):
    score, feats, h_diff, c_diff = score_pair(r.kb_id, r.seoul_code, r.distance_m)
    scored.append({
        "kb_id":      r.kb_id,
        "seoul_code": r.seoul_code,
        "distance_m": r.distance_m,
        "score":      score,
        "feat_address":    feats["address"],
        "feat_name":       feats["name"],
        "feat_household":  feats["household"],
        "feat_completion": feats["completion"],
        "household_diff":        h_diff,
        "completion_diff_months": c_diff,
    })

scored_df = pd.DataFrame(scored).sort_values("score", ascending=False).reset_index(drop=True)
print("스코어링 완료:", scored_df.shape)
scored_df.head()


# ## 6. 매칭 결정 (greedy)
# 
# score 내림차순 순회하며 이미 쇌 쓰인 kb_id/seoul_code는 skip.
# 임계값 테이블 순서대로 method를 고르고, 차순위 페어와 score 차이가 ε 이내면 needs_review=1.

# In[9]:


def classify(distance_m, score):
    for max_d, min_s, method, nr in THRESHOLDS:
        if distance_m <= max_d and score >= min_s:
            return method, nr
    return None, None

# kb_id, seoul_code별 score 목록 (차순위 검사용)
kb_scores, se_scores = {}, {}
for r in scored_df.itertuples(index=False):
    kb_scores.setdefault(r.kb_id, []).append(r.score)
    se_scores.setdefault(r.seoul_code, []).append(r.score)

used_kb, used_se = set(), set()
matches = []

for r in scored_df.itertuples(index=False):
    if r.kb_id in used_kb or r.seoul_code in used_se:
        continue
    method, nr = classify(r.distance_m, r.score)
    if method is None:
        continue
    # 차순위 고려
    next_kb = max([s for s in kb_scores[r.kb_id]      if s < r.score] or [0])
    next_se = max([s for s in se_scores[r.seoul_code] if s < r.score] or [0])
    if (r.score - max(next_kb, next_se)) < EPSILON_REVIEW:
        nr = 1
    matches.append({
        "kb_id":        r.kb_id,
        "seoul_code":   r.seoul_code,
        "distance_m":   r.distance_m,
        "score":        r.score,
        "method":       method,
        "needs_review": nr,
        "feat_address":    r.feat_address,
        "feat_name":       r.feat_name,
        "feat_household":  r.feat_household,
        "feat_completion": r.feat_completion,
        "household_diff":         r.household_diff,
        "completion_diff_months": r.completion_diff_months,
    })
    used_kb.add(r.kb_id)
    used_se.add(r.seoul_code)

matched_df = pd.DataFrame(matches)
print("매칭 결과:", matched_df.shape)
if not matched_df.empty:
    print(matched_df["method"].value_counts())
    print("needs_review:", int(matched_df["needs_review"].sum()))


# ## 7. 잔여 단지 처리
# 
# 매칭 안 된 KB 몇 서울시 단지를 식별.

# In[10]:


matched_kb = set(matched_df["kb_id"]) if not matched_df.empty else set()
matched_se = set(matched_df["seoul_code"]) if not matched_df.empty else set()

kb_only_df    = kb_df[~kb_df["단지기본일련번호"].isin(matched_kb)].copy()
seoul_only_df = seoul_df[~seoul_df["complex_code"].isin(matched_se)].copy()
print(f"kb_only: {len(kb_only_df)} / seoul_only: {len(seoul_only_df)}")


# ## 8. complex_master 생성
# 
# matched/seoul_only/kb_only 3가지를 하나의 DataFrame으로 합친다.
# matched 행은 KB 값 우선, NULL이면 서울시로 fallback.
# 좌표가 NULL/0인 행은 제외.

# In[11]:


def pick(first, second):
    """첫 번째 유효값 우선, 없으면 두 번째로 fallback. 둘 다 무효면 None."""
    for v in (first, second):
        if v is not None and pd.notna(v) and v != "" and v != 0:
            return v
    return None

def pick_max(*values):
    """결측/0 제외한 값들 중 최댓값. 모두 무효면 None.
    세대수처럼 '부분 vs 전체 단지' 차이가 있을 때 전체(큰 값) 채택용."""
    valid = [v for v in values if v is not None and pd.notna(v) and v != 0]
    return max(valid) if valid else None

def normalize_completion(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    digits = re.sub(r"\D", "", str(v))
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return None

def valid_coord(lat, lng):
    return pd.notna(lat) and pd.notna(lng) and lat != 0 and lng != 0

print("선택 함수 준비 완료")


# In[12]:


master_rows = []

# matched: canonical_name은 서울시 우선(더 정제된 표기),
#          household_count는 큰 값(부분 vs 전체 단지 차이 보정),
#          좌표/주소/준공일/총동수는 KB 우선
for r in matched_df.itertuples(index=False):
    kb = kb_idx.loc[r.kb_id]
    se = seoul_idx.loc[r.seoul_code]
    lat = pick(kb.get("wgs84위도"), se.get("latitude"))
    lng = pick(kb.get("wgs84경도"), se.get("longitude"))
    if not valid_coord(lat, lng):
        continue
    master_rows.append({
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

# seoul_only
for _, se in seoul_only_df.iterrows():
    lat, lng = se.get("latitude"), se.get("longitude")
    if not valid_coord(lat, lng):
        continue
    master_rows.append({
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

# kb_only
for _, kb in kb_only_df.iterrows():
    lat, lng = kb.get("wgs84위도"), kb.get("wgs84경도")
    if not valid_coord(lat, lng):
        continue
    master_rows.append({
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

master_df = pd.DataFrame(master_rows)
print("master 행 수:", len(master_df))
print(master_df["data_source"].value_counts())


# In[13]:


CREATE_MASTER = """
CREATE TABLE complex_master (
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

INSERT_COLS = [
    "canonical_name", "road_address", "legal_address",
    "latitude", "longitude",
    "household_count", "completion_date", "total_buildings",
    "seoul_complex_code", "kb_complex_id", "data_source", "built_at",
]

def to_int_or_none(v):
    return int(v) if (v is not None and pd.notna(v)) else None

def to_text_or_none(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s else None

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

placeholders = ",".join(["?"] * len(INSERT_COLS))
with sqlite3.connect(OUT_DB) as conn:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS complex_master")
    cur.execute(CREATE_MASTER)
    cur.executemany(
        f"INSERT INTO complex_master ({','.join(INSERT_COLS)}) VALUES ({placeholders})",
        rows_for_insert,
    )
    conn.commit()
    master_with_id = pd.read_sql(
        "SELECT master_complex_id, seoul_complex_code, kb_complex_id, data_source "
        "FROM complex_master",
        conn,
    )
print("complex_master 저장 완료:", len(master_with_id))


# ## 9. complex_match_log 생성
# 
# master 각 행마다 대응되는 매칭 의사결정 로그를 1행씩 작성. master ↔ log 는 1:1.

# In[14]:


matched_lookup = (
    matched_df.set_index(["kb_id", "seoul_code"])
    if not matched_df.empty else None
)

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
            "household_diff":         int(mr["household_diff"]) if pd.notna(mr["household_diff"]) else None,
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

CREATE_LOG = """
CREATE TABLE complex_match_log (
    log_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    master_complex_id       INTEGER REFERENCES complex_master(master_complex_id),
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

with sqlite3.connect(OUT_DB) as conn:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS complex_match_log")
    cur.execute(CREATE_LOG)
    log_df.to_sql("complex_match_log", conn, if_exists="append", index=False)
    conn.commit()
print("complex_match_log 저장 완료:", len(log_df))


# ## 10. 인덱스 생성

# In[15]:


INDEX_STMTS = [
    "CREATE INDEX IF NOT EXISTS idx_master_lat        ON complex_master(latitude)",
    "CREATE INDEX IF NOT EXISTS idx_master_lng        ON complex_master(longitude)",
    "CREATE INDEX IF NOT EXISTS idx_master_seoul_code ON complex_master(seoul_complex_code)",
    "CREATE INDEX IF NOT EXISTS idx_master_kb_id      ON complex_master(kb_complex_id)",
    "CREATE INDEX IF NOT EXISTS idx_inv_kb_id         ON kb_investment_raw(단지기본일련번호)",
    "CREATE INDEX IF NOT EXISTS idx_log_master        ON complex_match_log(master_complex_id)",
    "CREATE INDEX IF NOT EXISTS idx_log_review        ON complex_match_log(needs_review) WHERE needs_review = 1",
]

with sqlite3.connect(OUT_DB) as conn:
    cur = conn.cursor()
    for stmt in INDEX_STMTS:
        cur.execute(stmt)
    conn.commit()
print("인덱스 생성 완료")


# ## 11. 검증
# 
# - 테이블 행 수
# - `data_source` 분포
# - `match_method` 분포
# - needs_review 미리보기
# - master → kb_investment_raw JOIN 동작 확인

# In[16]:


with sqlite3.connect(OUT_DB) as conn:
    print("=== 테이블 행 수 ===")
    for tbl in ["seoul_complex_raw", "kb_complex_raw", "kb_investment_raw",
                "complex_master", "complex_match_log"]:
        n = pd.read_sql(f"SELECT COUNT(*) AS n FROM {tbl}", conn)["n"].iloc[0]
        print(f"  {tbl}: {n}")

    print("\n=== data_source 분포 ===")
    print(pd.read_sql(
        "SELECT data_source, COUNT(*) AS n FROM complex_master GROUP BY data_source",
        conn,
    ))

    print("\n=== match_method 분포 ===")
    print(pd.read_sql("""
        SELECT match_method,
               COUNT(*) AS n,
               ROUND(AVG(distance_meter), 2) AS avg_dist_m,
               ROUND(AVG(match_score), 3)    AS avg_score
          FROM complex_match_log
         GROUP BY match_method
         ORDER BY n DESC
    """, conn))

    print("\n=== needs_review 미리보기 (최대 20건) ===")
    print(pd.read_sql("""
        SELECT log.master_complex_id, m.canonical_name,
               ROUND(log.distance_meter, 1)    AS dist_m,
               ROUND(log.match_score, 3)       AS score,
               ROUND(log.name_similarity, 2)   AS name_sim,
               ROUND(log.address_similarity,2) AS addr_sim,
               log.match_method
          FROM complex_match_log log
          JOIN complex_master    m  ON m.master_complex_id = log.master_complex_id
         WHERE log.needs_review = 1
         ORDER BY log.match_score DESC
         LIMIT 20
    """, conn).to_string())

    print("\n=== JOIN 동작 확인 (시세 행 수 상위 10) ===")
    print(pd.read_sql("""
        SELECT m.canonical_name, COUNT(i.면적일련번호) AS price_rows
          FROM complex_master m
          JOIN kb_investment_raw i ON i.단지기본일련번호 = m.kb_complex_id
         WHERE m.kb_complex_id IS NOT NULL
         GROUP BY m.master_complex_id
         ORDER BY price_rows DESC
         LIMIT 10
    """, conn))

