import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

REST_API_KEY = os.getenv("KAKAO_REST_API_KEY")
if not REST_API_KEY:
    raise RuntimeError("KAKAO_REST_API_KEY가 .env 파일에 설정되어 있지 않습니다.")

url = "https://dapi.kakao.com/v2/local/search/address.json"

headers = {
    "Authorization": f"KakaoAK {REST_API_KEY}"
}

params = {
    "query": "서울특별시 강남구 테헤란로 152"
}

response = requests.get(url, headers=headers, params=params)

print(response.status_code)
print(json.dumps(response.json(), ensure_ascii=False, indent=2))

import pandas as pd

headers = {
    "Authorization": f"KakaoAK {REST_API_KEY}"
}

# 강남파이낸스센터 좌표
CENTER_X = "127.036508620542"   # 경도
CENTER_Y = "37.5000242405515"   # 위도

# 반경 100m
RADIUS = 100

# 카카오 Local API 카테고리 그룹 전체
CATEGORY_CODES = {
    "대형마트": "MT1",
    "편의점": "CS2",
    "어린이집_유치원": "PS3",
    "학교": "SC4",
    "학원": "AC5",
    "주차장": "PK6",
    "주유소_충전소": "OL7",
    "지하철역": "SW8",
    "은행": "BK9",
    "문화시설": "CT1",
    "중개업소": "AG2",
    "공공기관": "PO3",
    "관광명소": "AT4",
    "숙박": "AD5",
    "음식점": "FD6",
    "카페": "CE7",
    "병원": "HP8",
    "약국": "PM9",
}

url = "https://dapi.kakao.com/v2/local/search/category.json"

all_places = []

for category_name, category_code in CATEGORY_CODES.items():
    print(f"\n[{category_name}] 조회 시작")

    for page in range(1, 4):
        params = {
            "category_group_code": category_code,
            "x": CENTER_X,
            "y": CENTER_Y,
            "radius": RADIUS,
            "sort": "distance",
            "page": page,
            "size": 15,
        }

        response = requests.get(url, headers=headers, params=params)

        print(f"{category_name} page {page} status:", response.status_code)

        if response.status_code != 200:
            print(response.text)
            break

        data = response.json()
        documents = data.get("documents", [])

        for place in documents:
            all_places.append({
                "조회카테고리": category_name,
                "카테고리코드": category_code,
                "장소명": place.get("place_name"),
                "카카오상세카테고리": place.get("category_name"),
                "거리_m": place.get("distance"),
                "도로명주소": place.get("road_address_name"),
                "지번주소": place.get("address_name"),
                "전화번호": place.get("phone"),
                "장소URL": place.get("place_url"),
                "x": place.get("x"),
                "y": place.get("y"),
            })

        if data.get("meta", {}).get("is_end"):
            break

df = pd.DataFrame(all_places)

print("\n===== 전체 결과 미리보기 =====")
print(df.head(30))

print("\n===== 카테고리별 개수 =====")

if len(df) > 0:
    category_count = df["조회카테고리"].value_counts()
    print(category_count)
else:
    print("조회된 장소가 없습니다.")

# CSV 저장
csv_filename = "gangnam_finance_center_100m_all_categories.csv"
df.to_csv(csv_filename, index=False, encoding="utf-8-sig")

print(f"\nCSV 저장 완료: {csv_filename}")