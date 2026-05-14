import os
import re
import requests
from dotenv import load_dotenv

#pip install python-dotenv 설치 필요

load_dotenv()

SERVICE_KEY = os.getenv("SERVICE_KEY")

BASE_URL = "https://api.odcloud.kr/api/15003214/v1/uddi:590f870b-8aef-4ac0-a08e-a36bbbdb01ec"

# 모듈 레벨 캐시: 첫 호출 때 한 번만 API에서 받아오고, 이후 호출은 메모리에서 재사용
_GSEED_CACHE = None


def normalize_text(value):
    if value is None:
        return ""

    text = str(value)
    text = text.lower()
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", "", text)
    return text


def fetch_all_gseed(per_page=1000, force_refresh=False):
    global _GSEED_CACHE

    # 캐시 히트: 이미 받아둔 결과가 있으면 API 재호출 없이 반환
    if _GSEED_CACHE is not None and not force_refresh:
        return _GSEED_CACHE

    all_rows = []
    page = 1

    while True:
        params = {
            "page": page,
            "perPage": per_page,
            "serviceKey": SERVICE_KEY,
            "returnType": "JSON",
        }

        response = requests.get(BASE_URL, params=params, timeout=10)

        print(f"{page}페이지 요청 상태 코드:", response.status_code)

        if response.status_code != 200:
            # 실패 응답은 캐시하지 않음 (다음 호출에서 재시도 가능)
            return {
                "success": False,
                "error_status_code": response.status_code,
                "error_text": response.text,
                "data": []
            }

        result = response.json()

        rows = result.get("data", [])
        total_count = result.get("totalCount", 0)

        if not rows:
            break

        all_rows.extend(rows)

        if len(all_rows) >= total_count:
            break

        page += 1

    result = {
        "success": True,
        "error_status_code": None,
        "error_text": None,
        "data": all_rows
    }

    # 성공한 결과만 캐시에 저장
    _GSEED_CACHE = result
    return result


def select_best_gseed_result(results):
    """
    검색 결과가 여러 개일 때 대표 결과 선택

    우선순위:
    1. 본인증
    2. 예비인증
    3. 그 외 첫 번째 결과
    """

    # API 호출 실패 또는 인증 미확인처럼 dict로 반환된 경우
    if isinstance(results, dict):
        return results

    if not results:
        return None

    # 1순위: 본인증
    for item in results:
        if item.get("인증구분") == "본인증":
            return item

    # 2순위: 예비인증
    for item in results:
        if item.get("인증구분") == "예비인증":
            return item

    # 3순위: 첫 번째 결과
    return results[0]


def get_gseed_grade_api(address_keyword):
    fetch_result = fetch_all_gseed()

    if not fetch_result["success"]:
        return {
            "검색_주소": address_keyword,
            "gseed_status": "API 호출 실패",
            "gseed_등급": None,
            "source": "공공데이터포털 G-SEED 녹색건축 인증현황 API",
            "error_status_code": fetch_result["error_status_code"],
            "error_text": fetch_result["error_text"],
            "note": (
                "API 호출 자체가 실패했습니다. "
                "따라서 해당 건물의 G-SEED 인증 여부를 판단할 수 없습니다. "
                "공공데이터포털에서 해당 API 활용신청이 승인되었는지, "
                "현재 사용 중인 인증키가 이 API에 등록되어 있는지 확인해야 합니다."
            )
        }

    rows = fetch_result["data"]

    address_key = normalize_text(address_keyword)

    matched = []

    for row in rows:
        building_name = (
            row.get("건축물명")
            or row.get("건물명")
            or ""
        )

        address = (
            row.get("건축물 주소")
            or row.get("건축물주소")
            or row.get("주소")
            or ""
        )

        # 주소 필드만 대상으로 매칭 (건물명 등 다른 필드에 우연히 포함된 경우 제외)
        address_norm = normalize_text(address)

        if address_key not in address_norm:
            continue

        matched.append({
            "건축물명": building_name,
            "주소": address,
            "gseed_status": "인증 확인",
            "gseed_등급": row.get("인증등급"),
            "인증구분": row.get("인증구분"),
            "인증기준": row.get("인증기준"),
            "인증일자": row.get("인증일자"),
            "인증번호": row.get("인증번호"),
            "원본데이터": row,
            "source": "공공데이터포털 G-SEED 녹색건축 인증현황 API",
            "note": "공공데이터포털 G-SEED 인증현황 API에서 조회됨"
        })

    if not matched:
        return {
            "검색_주소": address_keyword,
            "gseed_status": "인증 미확인",
            "gseed_등급": None,
            "source": "공공데이터포털 G-SEED 녹색건축 인증현황 API",
            "note": (
                "API 호출은 성공했지만, 해당 조건에 맞는 G-SEED 인증 기록을 찾지 못했습니다. "
                "이는 G-SEED 인증 기록을 확인하지 못했다는 의미이며, "
                "친환경 인증이 전혀 없다는 뜻은 아닙니다."
            )
        }

    return matched


# 실행 예시
results = get_gseed_grade_api("부산광역시 기장군 기장읍 시랑리 산46")

print("\n전체 검색 결과:")
print(results)

best_result = select_best_gseed_result(results)

print("\n대표 결과:")
print(best_result)
