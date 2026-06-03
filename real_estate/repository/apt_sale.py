"""아파트 매매 실거래가 repository.

`apt_sale_transaction` 테이블 위에서 단지/지역 기준 조회 헬퍼만 제공.
location → master_complex_id 해석은 호출자(서비스/에이전트 툴 계층)가 책임진다.
"""

import logging
import sqlite3

logger = logging.getLogger("repository.apt_sale")


class AptSaleTransactionRepository:
    def __init__(self, db_path: str):
        self._db_path = db_path

    def fetch_by_complex_id(
        self,
        master_complex_id: int,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """특정 마스터 단지의 최근 매매 거래 N건. 최신순."""
        conn = sqlite3.connect(self._db_path)
        try:
            cur = conn.execute(
                """
                SELECT deal_year_month, deal_day, deal_amount, apt_name,
                       area_sqm, floor, build_year, jibun, is_canceled, cancel_date
                  FROM apt_sale_transaction
                 WHERE master_complex_id = ?
                   AND is_canceled = 0
                 ORDER BY deal_year_month DESC, deal_day DESC
                 LIMIT ?
                """,
                (master_complex_id, limit),
            )
            return [_row_to_dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def fetch_recent_in_lawd(
        self,
        lawd_cd: str,
        *,
        ymd_from: str,
        ymd_to: str,
        limit: int = 500,
    ) -> list[dict]:
        """특정 시군구의 거래 내역. ymd 범위 inclusive (YYYYMM)."""
        conn = sqlite3.connect(self._db_path)
        try:
            cur = conn.execute(
                """
                SELECT deal_year_month, deal_day, deal_amount, apt_name,
                       area_sqm, floor, build_year, jibun, is_canceled, cancel_date,
                       master_complex_id
                  FROM apt_sale_transaction
                 WHERE lawd_cd = ?
                   AND deal_year_month BETWEEN ? AND ?
                   AND is_canceled = 0
                 ORDER BY deal_year_month DESC, deal_day DESC
                 LIMIT ?
                """,
                (lawd_cd, ymd_from, ymd_to, limit),
            )
            return [_row_to_dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def summary_by_complex_id(self, master_complex_id: int) -> dict:
        """단지별 거래 요약 (건수, 평균 금액, 최근 거래일)."""
        conn = sqlite3.connect(self._db_path)
        try:
            cur = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       AVG(deal_amount) AS avg_amount,
                       MAX(deal_year_month || printf('%02d', COALESCE(deal_day, 0))) AS latest_ymd
                  FROM apt_sale_transaction
                 WHERE master_complex_id = ?
                   AND is_canceled = 0
                """,
                (master_complex_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None or row[0] == 0:
            return {"transaction_count": 0, "avg_amount": None, "latest_deal_ymd": None}
        n, avg_amount, latest = row
        return {
            "transaction_count": int(n),
            "avg_amount": float(avg_amount) if avg_amount is not None else None,
            "latest_deal_ymd": latest,
        }


def _row_to_dict(row) -> dict:
    cols = [
        "deal_year_month", "deal_day", "deal_amount", "apt_name",
        "area_sqm", "floor", "build_year", "jibun", "is_canceled", "cancel_date",
    ]
    if len(row) == len(cols) + 1:
        cols.append("master_complex_id")
    return dict(zip(cols, row))
