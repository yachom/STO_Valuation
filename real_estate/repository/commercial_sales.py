"""상권 업종별 추정매출 조회.

서울시 상권분석서비스 '추정매출' 데이터(real_estate.db의
commercial_estimated_sales)를 읽어, 상권의 업종별 매출 구조를 반환한다.

이 데이터는 보고서의 commercial(상권·입지 활성도) 축에서 정량 grounding을
제공한다 — 뉴스 일화가 아니라 분기 단위 업종 매출 실수치다.

조회 키:
    trdar_cd(상권코드)가 자연 키다. 평가 대상 자산의 상권코드는 seed 단계에서
    이미 Neo4j 그래프(CommercialArea 노드)에 적재돼 있으므로, 보고서 흐름에서는
    그래프에서 trdar_cd를 읽어 이 repository로 넘긴다. 좌표만 있는 standalone
    호출은 fetch(location)이 CommercialAreaRepository로 상권을 해석한다.

반환 dict의 키:
    이 시스템의 데이터 소스 관례에 따라 반환 dict의 키는 한글이다
    (DB 내부 컬럼명은 영문이지만 fetch가 한글 키로 변환한다).
    cf. real_estate/repository/commercial_area.py
"""

import logging

from real_estate.location import LocationInfo
from real_estate.repository._db import connection

logger = logging.getLogger("repository.commercial_sales")


class CommercialSalesRepository:
    """상권 업종별 추정매출 조회 데이터 소스.

    사용법:
        repo = CommercialSalesRepository(db_path="DB/real_estate.db")
        result = repo.top_industries("3120119")        # 발산역(마곡)
        result = repo.fetch(location)                   # 좌표 → 상권 → 매출

    top_industries() 반환 dict 스키마:
        {
            "상권코드": str,
            "상권명": str,
            "기준분기": str,          # 최신 분기 (예: "20254")
            "분기수": int,            # 집계에 사용한 분기 수
            "업종수": int,            # 해당 상권 업종 개수
            "총매출": int,            # 최신 분기 전체 업종 매출 합 (원)
            "업종목록": [             # 매출 내림차순 top_n
                {
                    "업종코드": str,
                    "업종명": str,
                    "매출액": int,        # 최신 분기 매출 (원)
                    "매출건수": int,
                    "매출비중": float,    # 총매출 대비 (0~1)
                    "주중매출": int,
                    "주말매출": int,
                },
                # ...
            ],
        }
        상권코드에 데이터가 없으면 None.
    """

    def __init__(self, db_path):
        """Args: db_path — real_estate.db 파일 경로."""
        self._db_path = db_path

    def top_industries(self, trdar_cd: str, *, top_n: int = 10) -> dict | None:
        """한 상권의 최신 분기 업종별 매출 top_n을 반환한다.

        Args:
            trdar_cd: 상권코드.
            top_n:    반환할 상위 업종 수.

        Returns:
            업종 매출 dict (클래스 docstring 스키마). 데이터 없으면 None.
        """
        with connection(self._db_path) as conn:
            return self._top_industries(conn, trdar_cd, top_n)

    def fetch(self, location: LocationInfo, *, top_n: int = 10) -> dict:
        """좌표가 속한 상권(들)의 업종 매출을 반환한다 (standalone 편의).

        상권 매칭은 CommercialAreaRepository에 위임한다(좌표→상권 point-in-
        polygon 로직 재사용). 겹친 상권이 여럿이면 모두 담는다.

        Returns:
            {"매칭상권수": int, "상권목록": [top_industries() dict, ...]}
            어느 상권에도 안 속하면 상권목록은 빈 리스트.
        """
        from real_estate.repository.commercial_area import CommercialAreaRepository

        area_repo = CommercialAreaRepository(self._db_path)
        area_result = area_repo.fetch(location)
        codes = [a["상권코드"] for a in area_result.get("상권목록", [])
                 if a.get("상권코드")]

        with connection(self._db_path) as conn:
            areas = [self._top_industries(conn, cd, top_n) for cd in codes]
            areas = [a for a in areas if a is not None]
        return {"매칭상권수": len(areas), "상권목록": areas}

    # ── 내부 ──────────────────────────────────────────────────────
    def _top_industries(self, conn, trdar_cd: str, top_n: int) -> dict | None:
        cur = conn.cursor()

        # 최신 분기 확인. quarter_code는 "20254" 문자열이라 사전식=시간순.
        row = cur.execute(
            "SELECT max(quarter_code), max(trdar_name) FROM "
            "commercial_estimated_sales WHERE trdar_cd = ?", (trdar_cd,)
        ).fetchone()
        if not row or row[0] is None:
            return None
        latest_q, trdar_name = row

        rows = cur.execute("""
            SELECT industry_code, industry_name, sales_amount, sales_count,
                   weekday_amount, weekend_amount
            FROM commercial_estimated_sales
            WHERE trdar_cd = ? AND quarter_code = ?
            ORDER BY sales_amount DESC
        """, (trdar_cd, latest_q)).fetchall()
        if not rows:
            return None

        total = sum(r[2] or 0 for r in rows)
        industries = []
        for (icd, iname, amount, count, wd, we) in rows[:top_n]:
            industries.append({
                "업종코드": icd,
                "업종명": iname,
                "매출액": amount or 0,
                "매출건수": count or 0,
                "매출비중": round((amount or 0) / total, 4) if total else 0.0,
                "주중매출": wd or 0,
                "주말매출": we or 0,
            })

        return {
            "상권코드": trdar_cd,
            "상권명": trdar_name,
            "기준분기": latest_q,
            "분기수": 1,
            "업종수": len(rows),
            "총매출": total,
            "업종목록": industries,
        }
