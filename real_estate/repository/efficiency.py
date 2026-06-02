"""건축물 에너지효율등급 인증 DB의 SQLite 기반 구현.

LocationInfo를 입력으로 받아 해당 건물의 에너지효율등급(BEEC) 인증 정보를
building_certifications.db에서 조회해 dict로 반환한다.
"""

import logging
import sqlite3

from real_estate.location import LocationInfo

logger = logging.getLogger("repository.efficiency")


class EnergyEfficiencyGradeRepository:
    """건축물 에너지효율등급 인증 DB의 SQLite 기반 구현.

    다음을 가정한다:
        - building_certifications.db에 building_energy_grade 테이블이 있다.
        - jibun_addr (TEXT, 정규화된 지번 주소)를 조회 키로 쓴다.
        - 같은 지번에 인증이 여러 건이면 최신 1건을 고른다
          (본인증 > 예비인증 순, 같으면 인증일자 내림차순).

    fetch() 반환 스키마:
        {
            "energy_grade":       str | None,    # 에너지효율등급 (예: "1+++등급")
            "primary_energy_req": float | None,  # 1차에너지소요량
            "energy_reduce_rate": float | None,  # 에너지절감율
            "cert_type":          str | None,    # 인증종류 (본인증/예비인증)
            "cert_date":          str | None,    # 인증일자
            "building_name":      str | None,    # 건물명
            "building_category":  str | None,    # 건물용도
            "bdmgt_sn":           str | None,    # 건물관리번호 (25자리)
        }
    """

    def __init__(self, db_path):
        """구현체별 자원을 생성자에서 받는다.

        Args:
            db_path: SQLite DB 파일 경로 (building_certifications.db).
        """
        self._db_path = db_path

    def fetch(self, location: LocationInfo) -> dict:
        """추상 클래스가 약속한 dict 스키마에 맞춰 반환한다.

        Args:
            location: 정규화된 위치 정보 객체.
                      이 구현은 location.jibun_address를 조회 키로 쓴다.

        Returns:
            인증 정보 dict. 데이터가 없으면 모든 필드가 None인 dict.

        Raises:
            ValueError: location에 지번 주소가 없는 경우.
        """
        # 1) location에서 자기에게 필요한 필드만 꺼낸다.
        #    efficiency DB는 정규화된 지번 주소를 키로 쓴다.
        if location.jibun_address is None:
            # 필요한 키가 없으면 ValueError가 약속.
            raise ValueError(
                "EnergyEfficiencyGradeRepository는 지번 주소가 필요합니다."
            )

        # 2) 자기 DB에서 조회.
        #    같은 지번에 여러 인증이 있을 수 있어 최신 1건만 고른다.
        conn = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                """
                SELECT grd_name, primary_energy_req, energy_reduce_rate,
                       crtif_kind, crtif_isu_dd, bld_nm, purpose_kind_name,
                       bdmgt_sn
                  FROM building_energy_grade
                 WHERE jibun_addr = ?
                 ORDER BY
                     CASE crtif_kind
                         WHEN '본인증' THEN 1
                         WHEN '예비인증' THEN 2
                         ELSE 99
                     END,
                     crtif_isu_dd DESC
                 LIMIT 1
                """,
                (location.jibun_address,),
            )
            row = cursor.fetchone()
        finally:
            conn.close()

        # 3) 약속된 스키마에 맞춰 dict 조립.
        if row is None:
            # 데이터 없으면 모든 필드 None인 dict 반환.
            return {
                "energy_grade": None,
                "primary_energy_req": None,
                "energy_reduce_rate": None,
                "cert_type": None,
                "cert_date": None,
                "building_name": None,
                "building_category": None,
                "bdmgt_sn": None,
            }

        (energy_grade, primary_energy_req, energy_reduce_rate,
         cert_type, cert_date, building_name, building_category,
         bdmgt_sn) = row
        return {
            "energy_grade": energy_grade,
            "primary_energy_req": primary_energy_req,
            "energy_reduce_rate": energy_reduce_rate,
            "cert_type": cert_type,
            "cert_date": cert_date,
            "building_name": building_name,
            "building_category": building_category,
            "bdmgt_sn": bdmgt_sn,
        }
