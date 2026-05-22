"""STO 가치평가 시스템 진입점.

지금 단계에서는 다음만 한다:
    1. 사용자로부터 상가 주소를 입력받는다.
    2. LocationResolver로 외부 API들을 호출해 정규화한다.
    3. 결과(LocationInfo)를 화면에 출력한다.

실행:
    프로젝트 루트의 .env 파일에 KAKAO_API_KEY, JUSO_API_KEY를 적어두고
    $ python -m sto_valuation.main
"""

import os

from STO_Valuation.infra.env import load_env
from STO_Valuation.real_estate.location import LocationResolver


def main():
    """프로그램의 메인 흐름.

    아주 단순한 4단계:
        1. API 키들을 환경변수에서 읽는다.
        2. 사용자에게 주소를 입력받는다.
        3. LocationResolver를 만들어 resolve()를 호출한다.
        4. 결과를 출력한다.
    """
    # 1. .env 파일을 로드한 뒤 환경변수에서 API 키를 읽는다.
    # 코드에 키를 박지 않고 .env로 분리하는 것이 보안의 기본.
    load_env()
    kakao_api_key = os.environ.get("KAKAO_API_KEY")
    juso_api_key = os.environ.get("JUSO_API_KEY")

    if kakao_api_key is None:
        print("에러: KAKAO_API_KEY 환경변수를 설정해주세요.")
        return
    if juso_api_key is None:
        print("에러: JUSO_API_KEY 환경변수를 설정해주세요.")
        return

    # 2. 사용자 입력. input()은 stdin에서 한 줄을 받는 가장 기본 함수.
    raw_address = input("평가할 상가의 주소를 입력하세요: ")

    # 3. 리졸버 인스턴스를 만들고 정규화 실행.
    resolver = LocationResolver(
        kakao_api_key=kakao_api_key,
        juso_api_key=juso_api_key,
    )
    location01 = resolver.resolve(raw_address)

    # 4. 결과 출력. LocationInfo의 __repr__이 정의되어 있어 보기 좋게 나온다.
    print()
    print("=== 정규화 결과 ===")
    print(location01)

    print(location01.jibun_address)#지번주소
    print(location01.bd_mgt_sn)#건물관리번호


if __name__ == "__main__":
    main()
