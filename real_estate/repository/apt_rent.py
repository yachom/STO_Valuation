"""아파트 전월세 실거래가 repository.

`apt_rent_transaction` 테이블 위에서 단지/지역 기준 조회 헬퍼.
"""

import logging

from real_estate.repository._db import connection

logger = logging.getLogger("repository.apt_rent")


class AptRentTransactionRepository:
    def __init__(self, db_path: str):
        self._db_path = db_path

    def fetch_by_complex_id(
        self,
        master_complex_id: int,
        *,
        contract_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """특정 마스터 단지의 최근 전월세 계약 N건. 최신순."""
        sql = """
            SELECT deal_year_month, deal_day,
                   deposit, monthly_rent, contract_type, contract_period,
                   use_renewal_right, previous_deposit, previous_monthly_rent,
                   apt_name, area_sqm, floor, build_year, jibun
              FROM apt_rent_transaction
             WHERE master_complex_id = ?
        """
        params: list = [master_complex_id]
        if contract_type:
            sql += " AND contract_type = ?"
            params.append(contract_type)
        sql += " ORDER BY deal_year_month DESC, deal_day DESC LIMIT ?"
        params.append(limit)

        with connection(self._db_path) as conn:
            cur = conn.execute(sql, params)
            return [_row_to_dict(row) for row in cur.fetchall()]

    def jeonse_wolse_ratio(
        self,
        *,
        lawd_cd: str | None = None,
        master_complex_id: int | None = None,
        ymd_from: str | None = None,
        ymd_to: str | None = None,
    ) -> dict:
        """전세/월세 비율 — 테넌트 가치 핵심 지표.

        bcode/단지/기간 필터 중 적어도 하나는 지정 권장.
        """
        clauses = []
        params: list = []
        if lawd_cd is not None:
            clauses.append("lawd_cd = ?")
            params.append(lawd_cd)
        if master_complex_id is not None:
            clauses.append("master_complex_id = ?")
            params.append(master_complex_id)
        if ymd_from is not None:
            clauses.append("deal_year_month >= ?")
            params.append(ymd_from)
        if ymd_to is not None:
            clauses.append("deal_year_month <= ?")
            params.append(ymd_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with connection(self._db_path) as conn:
            cur = conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN contract_type = '전세' THEN 1 ELSE 0 END) AS jeonse,
                    SUM(CASE WHEN contract_type = '월세' THEN 1 ELSE 0 END) AS wolse,
                    AVG(CASE WHEN contract_type = '전세' THEN deposit END) AS avg_jeonse_deposit,
                    AVG(CASE WHEN contract_type = '월세' THEN deposit END) AS avg_wolse_deposit,
                    AVG(CASE WHEN contract_type = '월세' THEN monthly_rent END) AS avg_monthly_rent
                  FROM apt_rent_transaction
                {where}
                """,
                params,
            )
            row = cur.fetchone()

        jeonse = int(row[0] or 0)
        wolse = int(row[1] or 0)
        total = jeonse + wolse
        return {
            "jeonse_count": jeonse,
            "wolse_count": wolse,
            "jeonse_ratio": (jeonse / total) if total else None,
            "wolse_ratio": (wolse / total) if total else None,
            "avg_jeonse_deposit": float(row[2]) if row[2] is not None else None,
            "avg_wolse_deposit": float(row[3]) if row[3] is not None else None,
            "avg_monthly_rent": float(row[4]) if row[4] is not None else None,
        }


def _row_to_dict(row) -> dict:
    cols = [
        "deal_year_month", "deal_day",
        "deposit", "monthly_rent", "contract_type", "contract_period",
        "use_renewal_right", "previous_deposit", "previous_monthly_rent",
        "apt_name", "area_sqm", "floor", "build_year", "jibun",
    ]
    return dict(zip(cols, row))
