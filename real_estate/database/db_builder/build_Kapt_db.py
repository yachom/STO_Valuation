import os
import re
import time
import sqlite3

import pandas as pd
import requests
from dotenv import load_dotenv

EXCEL_PATH = "../raw_data/20260508_단지_기본정보.xlsx"

preview = pd.read_excel(EXCEL_PATH, header=None, nrows=5)
preview

HEADER_ROW = 1  # 위 preview를 보고 필요하면 0 또는 다른 값으로 조정

df = pd.read_excel(EXCEL_PATH, header=HEADER_ROW)
print("전체 행 수:", len(df))
print("컬럼 목록:", list(df.columns))
df.head()

seoul_df = df[df["시도"] == "서울특별시"].copy()
print("서울특별시 단지 수:", len(seoul_df))
seoul_df.head()

rename_map = {
    "단지코드": "complex_code",
    "단지명": "complex_name",
    "법정동주소": "legal_address",
    "도로명주소": "road_address",
    "세대수": "household_count",
    "사용승인일": "준공년월일",
}

seoul_df = seoul_df[list(rename_map.keys())].rename(columns=rename_map)
seoul_df.head()

load_dotenv()
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY")

if not KAKAO_REST_API_KEY:
    raise ValueError(".env 파일에 KAKAO_REST_API_KEY가 설정되어 있지 않습니다.")

print("API 키 로드 완료 (앞 4자리만 표시):", KAKAO_REST_API_KEY[:4] + "****")

KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
HEADERS = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}

geocode_cache = {}

# 지번(예: 302-13) 또는 도로명 번호(예: 123, 123-45) 토큰을 찾는 정규식.
# 공백 또는 문장 시작 뒤에 오고, 공백 또는 문장 끝 앞에 와야 함.
ADDRESS_TAIL_RE = re.compile(r"(?:^|\s)\d+(?:-\d+)?(?=\s|$)")


def clean_address(address):
    """콤마로 여러 주소가 붙어있을 때 첫 번째 주소만 남긴다."""
    if not address or pd.isna(address):
        return address
    if "," in address:
        return address.split(",")[0].strip()
    return address


def extract_address_only(address):
    """주소 뒤에 붙은 건물명/단지명을 제거하고 지번(또는 도로명 번호)까지만 남긴다.

    예: "서울특별시 은평구 불광동 302-13 루미노816청년안심주택"
        → "서울특별시 은평구 불광동 302-13"
    """
    if not address or pd.isna(address):
        return None
    matches = list(ADDRESS_TAIL_RE.finditer(address))
    if not matches:
        return None
    return address[: matches[-1].end()].strip()


def _kakao_search(url, query):
    """카카오 local API 공용 호출 함수. 성공 시 (lat, lng), 실패 시 (None, None)."""
    if not query or pd.isna(query):
        return None, None

    cache_key = (url, query)
    if cache_key in geocode_cache:
        return geocode_cache[cache_key]

    try:
        resp = requests.get(
            url,
            headers=HEADERS,
            params={"query": query},
            timeout=5,
        )
        resp.raise_for_status()
        documents = resp.json().get("documents", [])
        if documents:
            doc = documents[0]
            result = (float(doc["y"]), float(doc["x"]))
        else:
            result = (None, None)
    except Exception as e:
        print(f"[kakao 검색 실패] {query} → {e}")
        result = (None, None)

    geocode_cache[cache_key] = result
    return result


def geocode_one(address):
    """주소검색 API 호출."""
    return _kakao_search(KAKAO_ADDRESS_URL, address)


def keyword_one(name):
    """키워드검색 API 호출 (단지명용)."""
    return _kakao_search(KAKAO_KEYWORD_URL, name)


def geocode_row(road_address, legal_address, complex_name=None):
    """검색 순서:

    1) road 콤마 앞부분 → 2) road 정제 주소
    3) legal 콤마 앞부분 → 4) legal 정제 주소
    5) complex_name (키워드검색)

    정제 주소가 원본과 같거나 빈 값이면 후보에 추가하지 않는다.
    """
    candidates = []  # (query, status_label)

    cleaned_road = clean_address(road_address)
    if cleaned_road and not pd.isna(cleaned_road):
        candidates.append((cleaned_road, "success_road"))
        road_stripped = extract_address_only(cleaned_road)
        if road_stripped and road_stripped != cleaned_road:
            candidates.append((road_stripped, "success_road_stripped"))

    cleaned_legal = clean_address(legal_address)
    if cleaned_legal and not pd.isna(cleaned_legal):
        candidates.append((cleaned_legal, "success_legal"))
        legal_stripped = extract_address_only(cleaned_legal)
        if legal_stripped and legal_stripped != cleaned_legal:
            candidates.append((legal_stripped, "success_legal_stripped"))

    for query, label in candidates:
        lat, lng = geocode_one(query)
        if lat is not None:
            return lat, lng, query, label

    if complex_name and not pd.isna(complex_name):
        lat, lng = keyword_one(complex_name)
        if lat is not None:
            return lat, lng, complex_name, "success_name"

    return None, None, None, "fail"

latitudes = []
longitudes = []
queries = []
statuses = []

total = len(seoul_df)
for i, row in enumerate(seoul_df.itertuples(index=False), start=1):
    lat, lng, used_query, status = geocode_row(
        row.road_address,
        row.legal_address,
        row.complex_name,
    )
    latitudes.append(lat)
    longitudes.append(lng)
    queries.append(used_query)
    statuses.append(status)

    if i % 100 == 0 or i == total:
        print(f"진행 {i}/{total}")

    time.sleep(0.05)

seoul_df["latitude"] = latitudes
seoul_df["longitude"] = longitudes
seoul_df["geocoding_query"] = queries
seoul_df["geocoding_status"] = statuses

seoul_df.head()

DB_PATH = "../DB/complex_basic_info.db"
TABLE_NAME = "apt_complex_basic"

conn = sqlite3.connect(DB_PATH)
try:
    seoul_df.to_sql(TABLE_NAME, conn, if_exists="replace", index=False)
    conn.commit()
    print(f"저장 완료: {DB_PATH} / 테이블: {TABLE_NAME}")
finally:
    conn.close()

conn = sqlite3.connect(DB_PATH)
try:
    total_count = pd.read_sql(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_NAME}", conn
    )["cnt"].iloc[0]

    success_count = pd.read_sql(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_NAME} WHERE latitude IS NOT NULL",
        conn,
    )["cnt"].iloc[0]

    fail_count = pd.read_sql(
        f"SELECT COUNT(*) AS cnt FROM {TABLE_NAME} WHERE latitude IS NULL",
        conn,
    )["cnt"].iloc[0]

    print(f"전체 저장 건수: {total_count}")
    print(f"좌표 성공 건수: {success_count}")
    print(f"좌표 실패 건수: {fail_count}")

    top5 = pd.read_sql(f"SELECT * FROM {TABLE_NAME} LIMIT 5", conn)
finally:
    conn.close()

top5
