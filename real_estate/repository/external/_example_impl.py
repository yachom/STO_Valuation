"""구현 예시 (참고용 — 실제 데이터 없이 동작하는 더미).

새로운 데이터 소스를 맡은 팀원이 어떻게 자기 클래스를 작성해야 하는지
보여주기 위한 샘플 코드. 실제 사용하지 않으므로 production 코드에 포함시키지
않는다 (필요하면 examples/ 폴더 등으로 옮길 수 있다).

작성 단계:
    1. 자기 데이터의 추상 클래스를 import
    2. 그 클래스를 상속받는 자식 클래스를 만든다 (보통 이름 끝에 Impl 또는
       Sqlite, Api 같은 접미사)
    3. 생성자에서 자기 자원(DB 경로, API 키 등)을 받는다
    4. fetch(location) 메서드를 docstring 스키마에 맞춰 구현
"""

import sqlite3

from STO_Valuation.real_estate.location import LocationInfo
from STO_Valuation.real_estate.repository.base import BaseRepository

class EnvironmentGradeRepositorySqlite(BaseRepository):
    """환경등급 DB의 SQLite 기반 구현 (예시).

    다음을 가정한다:
        - environment.db에 grades 테이블이 있다.
        - 컬럼: address (TEXT, 도로명 주소), grade (TEXT), issued_date (TEXT).
    """

    def __init__(self, db_path):
        """구현체별 자원을 생성자에서 받는다.

        Args:
            db_path: SQLite DB 파일 경로.
        """
        self._db_path = db_path

    def fetch(self, location: LocationInfo) -> dict:
        """추상 클래스가 약속한 dict 스키마에 맞춰 반환한다.

        EnvironmentGradeRepository docstring 참고:
            {"grade": str | None, "issued_date": str | None,
             "rating_details": dict | None}
        """
        # 1) location에서 자기에게 필요한 필드만 꺼낸다.
        #    환경등급 DB는 도로명 주소를 키로 쓴다고 가정.
        if location.road_address is None:
            # 필요한 키가 없으면 ValueError가 약속.
            raise ValueError(
                "EnvironmentGradeRepository는 도로명 주소가 필요합니다."
            )

        # 2) 자기 DB에서 조회.
        conn = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT grade, issued_date FROM grades WHERE address = ?",
                (location.road_address,),
            )
            row = cursor.fetchone()
        finally:
            conn.close()

        # 3) 약속된 스키마에 맞춰 dict 조립.
        if row is None:
            # 데이터 없으면 모든 필드 None인 dict 반환.
            # 또는 팀 합의에 따라 LookupError를 던져도 됨.
            return {
                "grade": None,
                "issued_date": None,
                "rating_details": None,
            }

        grade, issued_date = row
        return {
            "grade": grade,
            "issued_date": issued_date,
            "rating_details": None,    # 이 구현은 세부 점수 미보유
        }


# 사용 측 코드 예시
# from sto_valuation.location import LocationResolver
# from sto_valuation.repositories.environment import EnvironmentGradeRepository
#
# # 1. 구현체 인스턴스 생성 (한 번)
# env_repo = EnvironmentGradeRepositorySqlite(db_path="repository/environment.db")
#
# # 2. location 만들기
# resolver = LocationResolver(api_key=...)
# location = resolver.resolve("서울 강북구 ...")
#
# # 3. 호출. 추상 클래스 타입으로 받아도 동작 (다형성)
# env_data: dict = env_repo.fetch(location)
# print(env_data["grade"])  # 예: "2등급"
