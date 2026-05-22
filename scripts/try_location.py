"""LocationResolver를 빠르게 시험해보는 스크립트.

팀원이 올린 컴포넌트를 리뷰/리팩토링한 뒤 동작을 확인할 때 사용한다.
같은 패턴으로 컴포넌트별 scripts/try_xxx.py를 추가해 쓰면 된다.

사용:
    python -m STO_Valuation.scripts.try_location "서울특별시 강남구 테헤란로 152"
    python -m STO_Valuation.scripts.try_location   # 인자 없으면 stdin으로 입력
"""

import os
import sys

from STO_Valuation.infra.env import load_env
from STO_Valuation.real_estate.location import LocationResolver


def main():
    load_env()
    kakao = os.environ.get("KAKAO_API_KEY")
    juso = os.environ.get("JUSO_API_KEY")
    if not kakao or not juso:
        sys.exit("KAKAO_API_KEY / JUSO_API_KEY를 .env에 설정하세요.")

    raw = " ".join(sys.argv[1:]).strip() or input("주소: ")
    resolver = LocationResolver(kakao_api_key=kakao, juso_api_key=juso)
    location = resolver.resolve(raw)
    print(location)


if __name__ == "__main__":
    main()
