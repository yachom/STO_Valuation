"""data_scout — 분석가가 호출하는 SQL 도구 라이브러리.

기존 retriever는 광범위한 의미 검색에 강하지만, 분석가가 '강남구 전세-월세
비율'처럼 매우 구체적 통계를 원할 땐 직접 SQL이 더 빠르고 정확하다.

이 모듈은 사전 정의된 scout 함수 셋을 제공한다. 각 함수는 (1) 1줄 한국어
설명, (2) 인자 명세, (3) 결과 텍스트 + ref를 가진다. 분석가는 자신이 받은
초기 evidence를 보고 어느 scout를 어떤 인자로 호출할지 LLM 선택을 한다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger("agent.data_scout")


@dataclass
class ScoutResult:
    text: str
    ref: str


class DataScout:
    def __init__(self, db_path: str):
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path)
        c.row_factory = sqlite3.Row
        return c

    # ─── tenant ──────────────────────────────────────────────
    def jeonse_wolse_ratio(self, *, sigungu: str | None = None,
                            master_complex_id: int | None = None) -> ScoutResult:
        """전세/월세 비율. 자치구 또는 단지 기준."""
        try:
            conn = self._conn()
            clauses, params = [], []
            if sigungu:
                clauses.append("legal_dong LIKE ?")
                params.append(f"%{sigungu}%")
            if master_complex_id is not None:
                clauses.append("master_complex_id = ?")
                params.append(master_complex_id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            r = conn.execute(
                f"""SELECT
                  SUM(CASE WHEN contract_type='전세' THEN 1 ELSE 0 END) AS j,
                  SUM(CASE WHEN contract_type='월세' THEN 1 ELSE 0 END) AS w,
                  COUNT(*) AS n
                FROM apt_rent_transaction {where}""",
                params,
            ).fetchone()
            conn.close()
            n = r["n"] or 0
            if not n:
                return ScoutResult("전월세 실거래 데이터 없음.",
                                   "scout:jeonse_wolse:no_data")
            j, w = r["j"] or 0, r["w"] or 0
            return ScoutResult(
                f"전월세 {n}건 — 전세 {j}건({j/n:.0%}) / 월세 {w}건({w/n:.0%}).",
                f"scout:jeonse_wolse:{sigungu or master_complex_id}",
            )
        except sqlite3.OperationalError:
            return ScoutResult("apt_rent_transaction 테이블 없음 (MOLIT 데이터 미적재).",
                               "scout:jeonse_wolse:missing_table")

    def public_lease_in_district(self, sigungu: str) -> ScoutResult:
        try:
            conn = self._conn()
            rows = conn.execute(
                "SELECT lease_type, COUNT(*) AS n, SUM(household_count) AS hh "
                "FROM public_lease_complex WHERE region_name LIKE ? "
                "GROUP BY lease_type ORDER BY n DESC",
                (f"%{sigungu}%",),
            ).fetchall()
            conn.close()
            if not rows:
                return ScoutResult(
                    f"{sigungu} LH 공공임대 데이터 없음.",
                    f"scout:lh:{sigungu}:no_data",
                )
            parts = [f"{r['lease_type']} {r['n']}단지({r['hh'] or 0:,}세대)" for r in rows]
            return ScoutResult(
                f"{sigungu} 공공임대 분포 — " + ", ".join(parts) + ".",
                f"scout:lh:{sigungu}",
            )
        except sqlite3.OperationalError:
            return ScoutResult(
                "public_lease_complex 테이블 없음.",
                "scout:lh:missing_table",
            )

    # ─── ESG ────────────────────────────────────────────────
    def cert_grade_distribution(self, sigungu: str) -> ScoutResult:
        conn = self._conn()
        rows = conn.execute(
            "SELECT grd_name, COUNT(*) AS n FROM energy_efficiency_cert "
            "WHERE jibun_addr LIKE ? OR full_addr LIKE ? "
            "GROUP BY grd_name ORDER BY grade_rank ASC LIMIT 10",
            (f"%{sigungu}%", f"%{sigungu}%"),
        ).fetchall()
        total = sum(r["n"] for r in rows) or 0
        if not total:
            conn.close()
            return ScoutResult(f"{sigungu} BEEC 인증 데이터 없음.",
                               f"scout:beec_dist:{sigungu}:no_data")
        beec_text = ", ".join(f"{r['grd_name']}({r['n']/total:.0%})" for r in rows[:5])
        # 상위 등급 비율 (1+++ + 1++ + 1+)
        top = conn.execute(
            "SELECT COUNT(*) AS n FROM energy_efficiency_cert "
            "WHERE (jibun_addr LIKE ? OR full_addr LIKE ?) "
            "AND grd_name IN ('1+++등급','1++등급','1+등급')",
            (f"%{sigungu}%", f"%{sigungu}%"),
        ).fetchone()
        conn.close()
        top_n = top["n"] if top else 0
        top_share = top_n / total if total else 0
        return ScoutResult(
            f"{sigungu} BEEC 인증 {total:,}건 (상위3등급 비중 {top_share:.0%}). 분포: {beec_text}.",
            f"scout:beec_dist:{sigungu}",
        )

    def gseed_grade_distribution(self, sigungu: str) -> ScoutResult:
        conn = self._conn()
        rows = conn.execute(
            "SELECT cert_grade, cert_type, COUNT(*) AS n FROM green_cert "
            "WHERE jibun_addr LIKE ? OR address LIKE ? "
            "GROUP BY cert_grade ORDER BY n DESC LIMIT 6",
            (f"%{sigungu}%", f"%{sigungu}%"),
        ).fetchall()
        conn.close()
        if not rows:
            return ScoutResult(f"{sigungu} G-SEED 인증 데이터 없음.",
                               f"scout:gseed:{sigungu}:no_data")
        total = sum(r["n"] for r in rows)
        dist = ", ".join(f"{r['cert_grade']}({r['n']})" for r in rows)
        return ScoutResult(
            f"{sigungu} G-SEED 인증 {total}건 — {dist}.",
            f"scout:gseed:{sigungu}",
        )

    def self_certified(self, canonical_name: str) -> ScoutResult:
        conn = self._conn()
        b = conn.execute(
            "SELECT bld_nm, grd_name, crtif_isu_dd FROM energy_efficiency_cert "
            "WHERE bld_nm LIKE ? LIMIT 3", (f"%{canonical_name}%",),
        ).fetchall()
        g = conn.execute(
            "SELECT building_name, cert_grade, cert_date FROM green_cert "
            "WHERE building_name LIKE ? LIMIT 3", (f"%{canonical_name}%",),
        ).fetchall()
        conn.close()
        if not b and not g:
            return ScoutResult(
                f"단지명({canonical_name})과 일치하는 인증 건물 미발견 (단지 자체 인증 가능성 낮음).",
                f"scout:self_cert:{canonical_name}:no_match",
            )
        parts = []
        if b:
            parts.append("BEEC 매칭: " + ", ".join(
                f"{r['bld_nm']}({r['grd_name']},{r['crtif_isu_dd']})" for r in b))
        if g:
            parts.append("G-SEED 매칭: " + ", ".join(
                f"{r['building_name']}({r['cert_grade']},{r['cert_date']})" for r in g))
        return ScoutResult(
            f"단지명 일치 인증 — " + " / ".join(parts) + ".",
            f"scout:self_cert:{canonical_name}",
        )

    # ─── Commercial ────────────────────────────────────────
    def commercial_industry_top(self, trdar_cd: str, n: int = 5) -> ScoutResult:
        conn = self._conn()
        rows = conn.execute(
            "SELECT industry_name, SUM(sales_amount) AS s, SUM(sales_count) AS c "
            "FROM commercial_estimated_sales WHERE trdar_cd=? "
            "GROUP BY industry_code ORDER BY s DESC LIMIT ?",
            (trdar_cd, n),
        ).fetchall()
        conn.close()
        if not rows:
            return ScoutResult("상권 매출 데이터 없음.",
                               f"scout:industry:{trdar_cd}:no_data")
        parts = [f"{r['industry_name']}({r['s']:,}원, {r['c']:,}건)" for r in rows]
        return ScoutResult(
            f"상권 {trdar_cd} 상위 {n}개 업종 누적 매출 — " + ", ".join(parts) + ".",
            f"scout:industry:{trdar_cd}",
        )

    def development_pipeline(self, sigungu: str) -> ScoutResult:
        conn = self._conn()
        rows = conn.execute(
            "SELECT category, stage, COUNT(*) AS n FROM development_project "
            "WHERE district = ? GROUP BY category, stage ORDER BY n DESC LIMIT 10",
            (sigungu,),
        ).fetchall()
        conn.close()
        if not rows:
            return ScoutResult(f"{sigungu} 정비사업 데이터 없음.",
                               f"scout:dev:{sigungu}:no_data")
        parts = [f"{r['category']}/{r['stage'] or '단계미상'}({r['n']})" for r in rows[:5]]
        total = sum(r["n"] for r in rows)
        return ScoutResult(
            f"{sigungu} 정비사업 총 {total}건. 주요: " + ", ".join(parts) + ".",
            f"scout:dev:{sigungu}",
        )


SCOUT_REGISTRY = [
    ("jeonse_wolse_ratio", "tenant", "전월세 비율 통계 (자치구 또는 단지)"),
    ("public_lease_in_district", "tenant", "자치구 LH 공공임대 분포"),
    ("cert_grade_distribution", "esg", "자치구 BEEC 등급 분포 + 상위3등급 비중"),
    ("gseed_grade_distribution", "esg", "자치구 G-SEED 등급 분포"),
    ("self_certified", "esg", "단지명으로 BEEC/G-SEED 직접 매칭"),
    ("commercial_industry_top", "commercial", "상권 상위 업종 매출"),
    ("development_pipeline", "commercial", "자치구 정비사업 분포"),
]
