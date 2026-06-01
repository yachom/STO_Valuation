"""Asset / RealEstate 클래스를 빠르게 시험해보는 스크립트.

부동산 자산을 한 건 만들어 보고, 통합 로거가 자산 컨텍스트와 함께
잘 동작하는지 확인한다. 내부에서 LocationResolver를 호출해 실제 주소를
정규화하므로 KAKAO/JUSO API 키가 .env에 있어야 한다.

사용 (프로젝트 루트 STO_valuation/ 안에서 실행):
    python -m scripts.try_asset "서울특별시 강남구 테헤란로 152"
    python -m scripts.try_asset   # 인자 없으면 stdin으로 입력
"""

import logging
import os
import sys

from infra.env import load_env
from real_estate.asset import RealEstate
from real_estate.location import LocationResolver


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    load_env()
    kakao = os.environ.get("KAKAO_API_KEY")
    juso = os.environ.get("JUSO_API_KEY")
    if not kakao or not juso:
        sys.exit("KAKAO_API_KEY / JUSO_API_KEY를 .env에 설정하세요.")

    raw = " ".join(sys.argv[1:]).strip() or input("주소: ")

    resolver = LocationResolver(kakao_api_key=kakao, juso_api_key=juso)
    real_estate = RealEstate.from_address(raw, resolver)

    print(real_estate)

    # 통합 로거 시연: 자산 컨텍스트(asset.real_estate.<asset_id>)가
    # 로거 이름에 자동으로 박힌다.
    logger = real_estate.get_logger()
    logger.info("자산 생성 완료")


if __name__ == "__main__":
    main()
