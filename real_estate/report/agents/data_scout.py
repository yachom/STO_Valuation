"""data_scout — 상가 부동산 평가용 SQL 통계 도구 라이브러리.

retriever는 광범위 의미 검색에 강하지만 분석가가 '강남구 분기 공실률 추이'
같은 매우 구체적 지표를 원할 땐 직접 SQL이 더 빠르고 정확하다.

각 scout 함수는 (1) 1줄 한국어 설명, (2) 인자 명세(`SCOUT_REGISTRY`),
(3) 결과 텍스트 + ref를 가진다. 분석가는 자신이 받은 초기 evidence를 보고
어느 scout를 어떤 인자로 호출할지 LLM으로 선택한다.

이전 버전(아파트 평가 기준)에서 다음을 폐기/교체:
- jeonse_wolse_ratio, public_lease_in_district → 상가 평가에 맞춰 제거.
- cert_grade_distribution/gseed_grade_distribution → 유지 (자치구 상대 위치 평가용).
- self_certified → `building_self_cert(bd_mgt_sn)` 로 인자 단순화 (이전 환각의 핵심).
- commercial_industry_top, development_pipeline → 유지.

신규:
- commercial_sale_stats_in_district: MOLIT 상업·업무용 매매 자치구 통계.
- lease_market_summary: REB 공실률·임대료·수익률 (시군구·분기·건물유형).
- business_churn_rate: NTS 사업자 신규/폐업.
- transit_accessibility: 좌표 기반 가장 가까운 역 + 정류장 수.
- residential_demand: 좌표 기반 배후수요 (반경 안 아파트 단지 세대수).
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger("agent.data_scout")


@dataclass
class ScoutResult:
    text: str
    ref: str


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class DataScout:
    def __init__(self, db_path: str):
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path)
        c.row_factory = sqlite3.Row
        return c

    # ─── tenant ──────────────────────────────────────────────
    def commercial_sale_stats_in_district(self, *, sigungu_cd: str) -> ScoutResult:
        """MOLIT 상업·업무용 자치구 매매 통계 — 평균/건수/property_type 분포."""
        try:
            conn = self._conn()
            r = conn.execute(
                "SELECT COUNT(*) AS n, AVG(deal_amount) AS avg_amt, "
                "MIN(deal_year_month) AS first_q, MAX(deal_year_month) AS latest "
                "FROM commercial_sale_transaction WHERE lawd_cd=?",
                (sigungu_cd,),
            ).fetchone()
            dist = conn.execute(
                "SELECT property_type, COUNT(*) AS n FROM commercial_sale_transaction "
                "WHERE lawd_cd=? GROUP BY property_type ORDER BY n DESC LIMIT 5",
                (sigungu_cd,),
            ).fetchall()
            conn.close()
            if not (r and r["n"]):
                return ScoutResult(
                    f"자치구 {sigungu_cd} 상업·업무용 매매 데이터 없음.",
                    f"scout:csale:{sigungu_cd}:no_data",
                )
            dist_str = ", ".join(f"{r2['property_type'] or '미상'}({r2['n']})" for r2 in dist)
            return ScoutResult(
                f"자치구 {sigungu_cd} 상업·업무용 매매 {r['n']}건 "
                f"({r['first_q']}~{r['latest']}), 평균 {r['avg_amt']:,.0f}만원. "
                f"유형분포: {dist_str}.",
                f"scout:csale:{sigungu_cd}",
            )
        except sqlite3.OperationalError:
            return ScoutResult(
                "commercial_sale_transaction 테이블 없음 (MOLIT NRG 미적재).",
                "scout:csale:missing_table",
            )

    def lease_market_summary(
        self, *, sigungu_cd: str, building_type: str | None = None,
    ) -> ScoutResult:
        """REB 임대동향 — 자치구·건물유형의 최근 분기 공실률·임대료·수익률 + 추이."""
        try:
            conn = self._conn()
            clauses = ["sigungu_cd=?"]
            params: list = [sigungu_cd]
            if building_type:
                clauses.append("building_type=?")
                params.append(building_type)
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"""SELECT quarter_code, building_type, vacancy_rate, rent_per_sqm, yield_rate
                FROM commercial_lease_market_quarterly
                WHERE {where} ORDER BY quarter_code DESC LIMIT 6""",
                params,
            ).fetchall()
            conn.close()
            if not rows:
                return ScoutResult(
                    f"REB 임대동향 데이터 없음 (sigungu={sigungu_cd}).",
                    f"scout:reb:{sigungu_cd}:no_data",
                )
            latest = rows[0]
            trend = " → ".join(
                f"{r['quarter_code']}: 공실{r['vacancy_rate']}%/수익{r['yield_rate']}%"
                for r in reversed(rows[:4])
            )
            return ScoutResult(
                f"REB {sigungu_cd} {latest['building_type']} 최근 "
                f"({latest['quarter_code']}): 공실 {latest['vacancy_rate']}%, "
                f"임대료 {latest['rent_per_sqm']}원/㎡, 수익률 {latest['yield_rate']}%. "
                f"추이: {trend}.",
                f"scout:reb:{sigungu_cd}",
            )
        except sqlite3.OperationalError:
            return ScoutResult(
                "commercial_lease_market_quarterly 테이블 없음 (REB 미적재).",
                "scout:reb:missing_table",
            )

    def business_churn_rate(
        self, *, region_cd: str, region_level: str = "sigungu",
        recent_quarters: int = 1,
    ) -> ScoutResult:
        """NTS 사업자 신규/폐업/생존 — 최근 분기 폐업률 top."""
        try:
            conn = self._conn()
            rows = conn.execute(
                """SELECT industry_name, quarter_code,
                          new_count, closed_count, surviving_count,
                          CASE WHEN surviving_count>0
                               THEN ROUND(100.0 * closed_count / surviving_count, 2)
                               ELSE NULL END AS churn_pct
                FROM business_registry_stats_quarterly
                WHERE region_cd=? AND region_level=?
                  AND quarter_code IN (
                      SELECT DISTINCT quarter_code FROM business_registry_stats_quarterly
                      WHERE region_cd=? AND region_level=?
                      ORDER BY quarter_code DESC LIMIT ?)
                ORDER BY churn_pct DESC NULLS LAST LIMIT 5""",
                (region_cd, region_level, region_cd, region_level, recent_quarters),
            ).fetchall()
            conn.close()
            if not rows:
                return ScoutResult(
                    f"NTS 사업자 통계 없음 ({region_level}={region_cd}).",
                    f"scout:nts:{region_cd}:no_data",
                )
            lines = [
                f"{r['industry_name']}({r['quarter_code']}): "
                f"신규{r['new_count']}/폐업{r['closed_count']}/생존{r['surviving_count']} "
                f"= 폐업률 {r['churn_pct']}%"
                for r in rows
            ]
            return ScoutResult(
                f"NTS {region_cd}({region_level}) 폐업률 상위 5종: " + "; ".join(lines) + ".",
                f"scout:nts:{region_cd}",
            )
        except sqlite3.OperationalError:
            return ScoutResult(
                "business_registry_stats_quarterly 테이블 없음 (NTS 미적재).",
                "scout:nts:missing_table",
            )

    # ─── ESG ────────────────────────────────────────────────
    def building_self_cert(self, *, bd_mgt_sn: str) -> ScoutResult:
        """본 건물의 BEEC/G-SEED 직접 인증 — bd_mgt_sn 매칭. 단지명 추정 X."""
        try:
            conn = self._conn()
            b = conn.execute(
                "SELECT bld_nm, grd_name, crtif_isu_dd, primary_energy_req, energy_reduce_rate "
                "FROM energy_efficiency_cert WHERE bdmgt_sn=?",
                (bd_mgt_sn,),
            ).fetchall()
            g = conn.execute(
                "SELECT building_name, cert_grade, cert_type, cert_date, cert_standard "
                "FROM green_cert WHERE bdmgt_sn=?",
                (bd_mgt_sn,),
            ).fetchall()
            conn.close()
            if not b and not g:
                return ScoutResult(
                    f"본 건물(bd_mgt_sn={bd_mgt_sn}) BEEC/G-SEED 인증 미발견 "
                    f"(직접 인증 없음).",
                    f"scout:self_cert:{bd_mgt_sn}:no_match",
                )
            parts = []
            if b:
                parts.append("BEEC: " + "; ".join(
                    f"{r['bld_nm']}({r['grd_name']},{r['crtif_isu_dd']},"
                    f"절감률 {r['energy_reduce_rate']}%)" for r in b
                ))
            if g:
                parts.append("G-SEED: " + "; ".join(
                    f"{r['building_name']}({r['cert_grade']} {r['cert_type']},{r['cert_date']})"
                    for r in g
                ))
            return ScoutResult(
                f"본 건물 직접 인증 — " + " / ".join(parts) + ".",
                f"scout:self_cert:{bd_mgt_sn}",
            )
        except sqlite3.OperationalError:
            return ScoutResult(
                "cert 테이블 조회 실패.",
                "scout:self_cert:error",
            )

    def cert_grade_distribution(self, *, sigungu: str) -> ScoutResult:
        """자치구 BEEC 등급 분포 + 상위 3등급 비중 (상대 평가용)."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT grd_name, COUNT(*) AS n FROM energy_efficiency_cert "
            "WHERE jibun_addr LIKE ? OR full_addr LIKE ? "
            "GROUP BY grd_name ORDER BY grade_rank ASC LIMIT 10",
            (f"%{sigungu}%", f"%{sigungu}%"),
        ).fetchall()
        total = sum(r["n"] for r in rows)
        if not total:
            conn.close()
            return ScoutResult(
                f"{sigungu} BEEC 데이터 없음.",
                f"scout:beec_dist:{sigungu}:no_data",
            )
        top = conn.execute(
            "SELECT COUNT(*) AS n FROM energy_efficiency_cert "
            "WHERE (jibun_addr LIKE ? OR full_addr LIKE ?) "
            "AND grd_name IN ('1+++등급','1++등급','1+등급')",
            (f"%{sigungu}%", f"%{sigungu}%"),
        ).fetchone()
        conn.close()
        top_n = top["n"] if top else 0
        share = top_n / total if total else 0
        dist = ", ".join(f"{r['grd_name']}({r['n']/total:.0%})" for r in rows[:5])
        return ScoutResult(
            f"{sigungu} BEEC {total:,}건 (상위3등급 비중 {share:.0%}). 분포: {dist}.",
            f"scout:beec_dist:{sigungu}",
        )

    def gseed_grade_distribution(self, *, sigungu: str) -> ScoutResult:
        """자치구 G-SEED 등급 분포. 등급 위계: 최우수(그린1) > 우수(그린2) > 우량(그린3) > 일반(그린4)."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT cert_grade, COUNT(*) AS n FROM green_cert "
            "WHERE jibun_addr LIKE ? OR address LIKE ? "
            "GROUP BY cert_grade ORDER BY n DESC LIMIT 8",
            (f"%{sigungu}%", f"%{sigungu}%"),
        ).fetchall()
        conn.close()
        if not rows:
            return ScoutResult(
                f"{sigungu} G-SEED 데이터 없음.",
                f"scout:gseed:{sigungu}:no_data",
            )
        total = sum(r["n"] for r in rows)
        dist = ", ".join(f"{r['cert_grade']}({r['n']})" for r in rows)
        return ScoutResult(
            f"{sigungu} G-SEED {total}건. 분포: {dist}. "
            f"위계: 최우수(그린1)>우수(그린2)>우량(그린3)>일반(그린4).",
            f"scout:gseed:{sigungu}",
        )

    # ─── Commercial / 입지 ──────────────────────────────────
    def transit_accessibility(
        self, *, latitude: float, longitude: float,
    ) -> ScoutResult:
        """좌표 기준 가장 가까운 지하철역 + 반경 300/500m 버스정류장 수."""
        try:
            conn = self._conn()
            subs = conn.execute(
                "SELECT station_name, line_name, latitude, longitude FROM transit_station "
                "WHERE ABS(latitude-?)<0.02 AND ABS(longitude-?)<0.02",
                (latitude, longitude),
            ).fetchall()
            top_sub_text = ""
            if subs:
                subs_with_d = sorted(
                    subs,
                    key=lambda s: _haversine_m(latitude, longitude,
                                               s["latitude"], s["longitude"]),
                )
                top = subs_with_d[0]
                d = _haversine_m(latitude, longitude, top["latitude"], top["longitude"])
                top_sub_text = f"가장 가까운 역: {top['station_name']} ({top['line_name']}) {d:.0f}m. "
            bs = conn.execute(
                "SELECT latitude, longitude FROM bus_stop "
                "WHERE ABS(latitude-?)<0.005 AND ABS(longitude-?)<0.005",
                (latitude, longitude),
            ).fetchall()
            conn.close()
            within_300 = sum(
                1 for s in bs
                if _haversine_m(latitude, longitude, s["latitude"], s["longitude"]) <= 300
            )
            within_500 = sum(
                1 for s in bs
                if _haversine_m(latitude, longitude, s["latitude"], s["longitude"]) <= 500
            )
            return ScoutResult(
                f"{top_sub_text}반경 300m 버스정류장 {within_300}개 / 500m 내 {within_500}개.",
                f"scout:transit:{latitude:.4f},{longitude:.4f}",
            )
        except sqlite3.OperationalError:
            return ScoutResult(
                "transit_station/bus_stop 테이블 없음 (서울 교통 데이터 미적재).",
                "scout:transit:missing_table",
            )

    def residential_demand(
        self, *, latitude: float, longitude: float, radius_m: int = 800,
    ) -> ScoutResult:
        """좌표 기준 반경 안 아파트 단지 합산 세대수 + 행정동 상주인구 (배후수요)."""
        conn = self._conn()
        cur = conn.execute(
            "SELECT canonical_name, household_count, latitude, longitude, completion_date "
            "FROM apartment_complex WHERE latitude IS NOT NULL"
        ).fetchall()
        nearby = []
        for c in cur:
            d = _haversine_m(latitude, longitude, c["latitude"], c["longitude"])
            if d <= radius_m:
                nearby.append((d, c))
        conn.close()
        if not nearby:
            return ScoutResult(
                f"반경 {radius_m}m 안 아파트 단지 없음 — 배후수요 약함.",
                f"scout:backing:{latitude:.4f},{longitude:.4f}:no_data",
            )
        total_hh = sum(c["household_count"] or 0 for _, c in nearby)
        completions = [int(c["completion_date"][:4]) for _, c in nearby
                       if c["completion_date"]]
        avg_year = sum(completions) / len(completions) if completions else None
        top3 = ", ".join(
            f"{c['canonical_name']}({c['household_count']}세대,{d:.0f}m)"
            for d, c in sorted(nearby)[:3]
        )
        return ScoutResult(
            f"반경 {radius_m}m 배후수요 — 아파트 {len(nearby)}단지, "
            f"합산 {total_hh:,}세대, 평균 준공 {int(avg_year) if avg_year else '?'}년. "
            f"대표: {top3}.",
            f"scout:backing:{latitude:.4f},{longitude:.4f}",
        )

    def industry_top_in_area(self, *, trdar_cd: str, n: int = 5) -> ScoutResult:
        """상권의 상위 N개 업종 누적 매출 — 잠재 임차업종 가이드."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT industry_name, SUM(sales_amount) AS s, SUM(sales_count) AS c "
            "FROM commercial_estimated_sales WHERE trdar_cd=? "
            "GROUP BY industry_code ORDER BY s DESC LIMIT ?",
            (trdar_cd, n),
        ).fetchall()
        conn.close()
        if not rows:
            return ScoutResult(
                f"상권 {trdar_cd} 매출 데이터 없음.",
                f"scout:industry:{trdar_cd}:no_data",
            )
        parts = [f"{r['industry_name']}({r['s']:,}원,{r['c']:,}건)" for r in rows]
        return ScoutResult(
            f"상권 {trdar_cd} 상위 {n}개 업종 누적: " + ", ".join(parts) + ".",
            f"scout:industry:{trdar_cd}",
        )

    def development_pipeline(self, *, sigungu: str) -> ScoutResult:
        """자치구 정비사업 분포."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT category, stage, COUNT(*) AS n FROM development_project "
            "WHERE district=? GROUP BY category, stage ORDER BY n DESC LIMIT 10",
            (sigungu,),
        ).fetchall()
        conn.close()
        if not rows:
            return ScoutResult(
                f"{sigungu} 정비사업 데이터 없음.",
                f"scout:dev:{sigungu}:no_data",
            )
        total = sum(r["n"] for r in rows)
        parts = [f"{r['category']}/{r['stage'] or '단계미상'}({r['n']})" for r in rows[:5]]
        return ScoutResult(
            f"{sigungu} 정비사업 총 {total}건. 주요: " + ", ".join(parts) + ".",
            f"scout:dev:{sigungu}",
        )


SCOUT_REGISTRY = [
    ("commercial_sale_stats_in_district", "tenant",
     "MOLIT 상업·업무용 자치구 매매 통계 — 인자: sigungu_cd"),
    ("lease_market_summary", "tenant",
     "REB 공실률·임대료·수익률 — 인자: sigungu_cd, building_type(opt)"),
    ("business_churn_rate", "tenant",
     "NTS 사업자 신규/폐업 — 인자: region_cd, region_level('sigungu'|'adstrd')"),
    ("building_self_cert", "esg",
     "본 건물 BEEC/G-SEED 직접 인증 — 인자: bd_mgt_sn (25자리)"),
    ("cert_grade_distribution", "esg",
     "자치구 BEEC 등급 분포 + 상위3등급 비중 — 인자: sigungu(이름)"),
    ("gseed_grade_distribution", "esg",
     "자치구 G-SEED 등급 분포 — 인자: sigungu(이름)"),
    ("transit_accessibility", "commercial",
     "최근접 지하철역 + 300/500m 버스정류장 수 — 인자: latitude, longitude"),
    ("residential_demand", "commercial",
     "좌표 반경 안 아파트 세대수 합 (배후수요) — 인자: latitude, longitude, radius_m(opt)"),
    ("industry_top_in_area", "commercial",
     "상권 상위 N개 업종 매출 — 인자: trdar_cd, n(opt)"),
    ("development_pipeline", "commercial",
     "자치구 정비사업 분포 — 인자: sigungu(이름)"),
]
