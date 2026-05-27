"""STO 가치평가 시스템 진입점 (테스트용).

현재 단계에서 다음을 테스트한다:
    1. 주소 입력 → 카카오/Juso로 정규화 (LocationInfo 생성)
    2. 건축물대장 표제부 조회 (BuildingTitleRepository)

실행:
    $ python -m sto_valuation.main

API 키는 아래 상수에 직접 적어두었다 (테스트 단계). 실제 운영에서는
환경변수로 빼야 한다.
"""

from STO_Valuation.real_estate.location import LocationResolver, LocationInfo
from STO_Valuation.real_estate.repository.external.building_title import BuildingTitleRepository


# ─────────────────────────────────────────────────────────────────
# API 키 — 테스트용. 실제로는 환경변수로 관리할 것.
# ─────────────────────────────────────────────────────────────────
KAKAO_API_KEY = 'c1247d0f1113bb099f2b86bf2ef9a88f'
JUSO_API_KEY = "devU01TX0FVVEgyMDI2MDUyMDEwMjQwNDExODIwMDA="
BUILDING_SERVICE_KEY = "63b188c93b92003fb3593bd0ecc9f9cf367eac6335c247c460e0124442188bc8"


def test_full_flow():
    """전체 흐름 테스트: 주소 → 정규화 → 표제부 조회.

    주소를 입력받아 LocationResolver로 정규화한 뒤, 그 LocationInfo로
    건축물대장 표제부를 조회한다.
    """
    print("=" * 60)
    print("[전체 흐름 테스트]")
    print("=" * 60)

    raw_address = input("평가할 상가 주소를 입력하세요: ")

    # 1. 주소 정규화
    resolver = LocationResolver(
        kakao_api_key=KAKAO_API_KEY,
        juso_api_key=JUSO_API_KEY,
    )
    location = resolver.resolve(raw_address)

    print()
    print("--- 정규화 결과 (LocationInfo) ---")
    print(location)

    # 2. 건축물대장 표제부 조회
    building_repo = BuildingTitleRepository(service_key=BUILDING_SERVICE_KEY)
    facts = building_repo.fetch(location)

    print()
    print("--- 건축물대장 표제부 (BuildingTitleRepository) ---")
    _print_dict(facts)


def test_building_only():
    """건축물대장 표제부 조회만 단독 테스트.

    LocationResolver를 거치지 않고 LocationInfo를 손으로 만들어 테스트한다.
    카카오/Juso API 호출 없이 BuildingTitleRepository만 빠르게 확인할 때 쓴다.

    아래 값은 예시(서울 관악구 봉천동 21-15)다. 원하는 건물의
    법정동코드/본번/부번으로 바꿔서 테스트하면 된다.
    """
    print("=" * 60)
    print("[건축물대장 표제부 단독 테스트]")
    print("=" * 60)

    # LocationInfo를 손으로 만든다.
    # BuildingTitleRepository가 쓰는 건 bcode, bun, ji 세 개뿐이므로
    # 나머지 필드는 테스트용 더미 값으로 채운다.
    location = LocationInfo(
        raw_address="서울 관악구 관악로28길 20",
        road_address="서울특별시 관악구 관악로28길 20",
        jibun_address="서울특별시 관악구 봉천동 21-15",
        latitude=37.48,
        longitude=126.95,
        sido="서울특별시",
        sigungu="관악구",
        hcode="1162051000",
        hname="청룡동",
        bcode="1162010100",      # 법정동코드 10자리 → 앞5(시군구) 뒤5(법정동)
        bname="봉천동",
        bd_mgt_sn=None,
        bun="21",                # 본번
        ji="15",                 # 부번
    )

    print("--- 입력 LocationInfo ---")
    print("  bcode:", location.bcode, "→ 시군구",
          location.bcode[:5], "/ 법정동", location.bcode[5:])
    print("  bun:", location.bun, "/ ji:", location.ji)
    print()

    # 표제부 조회
    building_repo = BuildingTitleRepository(service_key=BUILDING_SERVICE_KEY)
    facts = building_repo.fetch(location)

    print("--- 건축물대장 표제부 결과 ---")
    _print_dict(facts)


def _print_dict(d):
    """dict를 키: 값 형태로 보기 좋게 출력한다."""
    for key, value in d.items():
        print(f"  {key}: {value}")


def main():
    """테스트 실행.

    아래 둘 중 하나를 골라서 실행한다:
        - test_building_only() : 표제부 조회만 단독 테스트 (LocationInfo 직접 생성,
                                 카카오/Juso 호출 없음)
        - test_full_flow()     : 주소 입력부터 전체 흐름 테스트
    """
    test_building_only()
    # test_full_flow()    # 전체 흐름을 테스트하려면 이쪽으로 전환


if __name__ == "__main__":
    main()
