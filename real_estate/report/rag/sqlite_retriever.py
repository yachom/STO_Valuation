"""SQLite-only Retriever (상가 부동산 평가 버전).

평가 대상은 상가 건물(주소 단위). AssetContext가 좌표·sigungu·bd_mgt_sn을
담고, 그 위치 기준으로 3축 candidate를 모은다:

- tenant: 임차 사업자 안정성. 시군구 REB 공실률/임대료, NTS 업종 churn,
  인접 commercial_area의 업종별 매출.
- esg: 본 건물 직접 BEEC/GSEED (bd_mgt_sn 매칭), 자치구 인증 분포로 상대 위치,
  건물 노후도 메타.
- commercial: 인접 commercial_area + 시계열, 반경 N m 안 아파트 단지 세대수
  (배후수요), admin_dong 상주 인구, 가장 가까운 지하철역/버스정류장, 자치구
  정비사업 + MOLIT 상가 매매 추이.

같은 Retriever Protocol을 만족하므로 에이전트 코드는 unchanged.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass

from real_estate.report.rag.embedder import embed_one, embed_texts
from real_estate.report.rag.protocol import Axis, GroundedSnippet, Retriever
from real_estate.report.tracing import emit_tool_call
from real_estate.report.tracing_sqlite import tracing_connect

logger = logging.getLogger("rag.sqlite_retriever")


@dataclass
class AssetContext:
    """상가 건물 평가 컨텍스트."""
    asset_id: str
    latitude: float | None = None
    longitude: float | None = None
    bd_mgt_sn: str | None = None
    building_name: str | None = None
    legal_address: str | None = None
    road_address: str | None = None
    sigungu: str | None = None
    sigungu_cd: str | None = None    # LAWD_CD 5자리, 우선 사용
    nearby_radius_m: int = 800       # 배후수요 반경


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _table_exists(conn, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


# ───────────────────────────────────────────────────────────
# axis별 candidate 수집 — 상가 평가 관점
# ───────────────────────────────────────────────────────────
def _candidates_tenant(conn, ctx: AssetContext) -> list[dict]:
    out: list[dict] = []

    # REB 시군구·분기 시장 (공실률/임대료/수익률)
    if _table_exists(conn, "commercial_lease_market_quarterly") and ctx.sigungu_cd:
        cur = conn.execute(
            "SELECT quarter_code, building_type, vacancy_rate, rent_per_sqm, yield_rate "
            "FROM commercial_lease_market_quarterly "
            "WHERE sigungu_cd=? ORDER BY quarter_code DESC LIMIT 8",
            (ctx.sigungu_cd,),
        ).fetchall()
        for r in cur:
            out.append({
                "text": (
                    f"REB 상업용 임대동향 — {ctx.sigungu} {r['quarter_code']} "
                    f"{r['building_type']}: 공실률 {r['vacancy_rate']}%, "
                    f"임대료 {r['rent_per_sqm']}원/㎡, 수익률 {r['yield_rate']}%."
                ),
                "ref": f"reb_lease:{ctx.sigungu_cd}:{r['quarter_code']}:{r['building_type']}",
                "kind": "reb_lease",
            })

    # NTS 사업자 신규/폐업 — 시군구 단위 최근 분기
    if _table_exists(conn, "business_registry_stats_quarterly") and ctx.sigungu_cd:
        cur = conn.execute(
            """
            SELECT industry_name, quarter_code,
                   new_count, closed_count, surviving_count,
                   CASE WHEN surviving_count > 0
                        THEN ROUND(100.0 * closed_count / surviving_count, 2)
                        ELSE NULL END AS churn_pct
            FROM business_registry_stats_quarterly
            WHERE region_cd=? AND region_level='sigungu'
              AND quarter_code = (SELECT MAX(quarter_code) FROM business_registry_stats_quarterly
                                  WHERE region_cd=? AND region_level='sigungu')
            ORDER BY churn_pct DESC NULLS LAST LIMIT 5
            """,
            (ctx.sigungu_cd, ctx.sigungu_cd),
        ).fetchall()
        for r in cur:
            out.append({
                "text": (
                    f"NTS 사업자 churn — {ctx.sigungu} {r['quarter_code']} "
                    f"{r['industry_name']}: 신규 {r['new_count']} / 폐업 {r['closed_count']} / "
                    f"생존 {r['surviving_count']} (폐업률 {r['churn_pct']}%)."
                ),
                "ref": f"nts:{ctx.sigungu_cd}:{r['industry_name']}",
                "kind": "nts_churn",
            })

    # 인접 commercial_area의 업종별 매출 분포 (배후 임차 수요 추정)
    if ctx.latitude and ctx.longitude:
        trdar_cd = _nearest_trdar(conn, ctx.latitude, ctx.longitude)
        if trdar_cd:
            cur = conn.execute(
                "SELECT industry_name, SUM(sales_amount) AS s "
                "FROM commercial_estimated_sales WHERE trdar_cd=? "
                "GROUP BY industry_code ORDER BY s DESC LIMIT 5",
                (trdar_cd,),
            ).fetchall()
            if cur:
                top = ", ".join(f"{r['industry_name']}({r['s']:,}원)" for r in cur)
                out.append({
                    "text": (
                        f"인접 상권({trdar_cd}) 누적 매출 상위 업종: {top}. "
                        f"본 건물의 잠재 임차업종 가이드."
                    ),
                    "ref": f"industry_top:{trdar_cd}",
                    "kind": "industry_top",
                })
    return out


def _candidates_esg(conn, ctx: AssetContext) -> list[dict]:
    out: list[dict] = []

    # ★ 건물 직접 인증 매칭 — bd_mgt_sn 우선, 없으면 단지명 매칭
    if ctx.bd_mgt_sn:
        for cur in (conn.execute(
            "SELECT bld_nm, grd_name, crtif_isu_dd, primary_energy_req, energy_reduce_rate "
            "FROM energy_efficiency_cert WHERE bdmgt_sn=?",
            (ctx.bd_mgt_sn,),
        ).fetchall()):
            out.append({
                "text": (
                    f"본 건물 BEEC 직접 인증 매칭 — {cur['bld_nm']}, "
                    f"등급 {cur['grd_name']}, 인증일 {cur['crtif_isu_dd']}, "
                    f"1차에너지소요량 {cur['primary_energy_req']}, "
                    f"절감률 {cur['energy_reduce_rate']}%."
                ),
                "ref": f"beec_self:{ctx.bd_mgt_sn}",
                "kind": "beec_self",
            })
        for cur in (conn.execute(
            "SELECT building_name, cert_grade, cert_type, cert_date, cert_standard "
            "FROM green_cert WHERE bdmgt_sn=?",
            (ctx.bd_mgt_sn,),
        ).fetchall()):
            out.append({
                "text": (
                    f"본 건물 G-SEED 직접 인증 매칭 — {cur['building_name']}, "
                    f"등급 {cur['cert_grade']} ({cur['cert_type']}), "
                    f"인증일 {cur['cert_date']}, 기준 {cur['cert_standard']}."
                ),
                "ref": f"gseed_self:{ctx.bd_mgt_sn}",
                "kind": "gseed_self",
            })

    # 자치구 인증 분포 (상대 위치 평가용)
    if ctx.sigungu:
        cur = conn.execute(
            "SELECT grd_name, COUNT(*) AS n FROM energy_efficiency_cert "
            "WHERE jibun_addr LIKE ? OR full_addr LIKE ? "
            "GROUP BY grd_name ORDER BY grade_rank ASC LIMIT 6",
            (f"%{ctx.sigungu}%", f"%{ctx.sigungu}%"),
        ).fetchall()
        if cur:
            total = sum(r["n"] for r in cur)
            dist = ", ".join(f"{r['grd_name']}({r['n']})" for r in cur)
            out.append({
                "text": f"{ctx.sigungu} BEEC 인증 분포 ({total}건): {dist}. (상대 위치 평가용)",
                "ref": f"beec_dist:{ctx.sigungu}",
                "kind": "beec_district",
            })
        cur = conn.execute(
            "SELECT cert_grade, COUNT(*) AS n FROM green_cert "
            "WHERE jibun_addr LIKE ? OR address LIKE ? "
            "GROUP BY cert_grade ORDER BY n DESC LIMIT 6",
            (f"%{ctx.sigungu}%", f"%{ctx.sigungu}%"),
        ).fetchall()
        if cur:
            total = sum(r["n"] for r in cur)
            dist = ", ".join(f"{r['cert_grade']}({r['n']})" for r in cur)
            out.append({
                "text": f"{ctx.sigungu} G-SEED 인증 분포 ({total}건): {dist}.",
                "ref": f"gseed_dist:{ctx.sigungu}",
                "kind": "gseed_district",
            })
    return out


def _candidates_commercial(conn, ctx: AssetContext) -> list[dict]:
    out: list[dict] = []
    if not (ctx.latitude and ctx.longitude):
        return out

    # 인접 상권 + 시계열
    trdar_cd = _nearest_trdar(conn, ctx.latitude, ctx.longitude)
    if trdar_cd:
        cur = conn.execute(
            "SELECT trdar_name, trdar_se_name, area_sqm, center_lat, center_lon "
            "FROM commercial_area WHERE trdar_cd=?",
            (trdar_cd,),
        ).fetchone()
        if cur:
            distance = _haversine_m(
                ctx.latitude, ctx.longitude, cur["center_lat"], cur["center_lon"],
            )
            out.append({
                "text": (
                    f"인접 상권 '{cur['trdar_name']}' ({cur['trdar_se_name']}), "
                    f"면적 {cur['area_sqm']:,.0f}㎡, 중심거리 {distance:.0f}m."
                ),
                "ref": f"trdar:{trdar_cd}",
                "kind": "trdar",
            })
        rows = conn.execute(
            "SELECT quarter_code, total FROM commercial_floating_population "
            "WHERE trdar_cd=? ORDER BY quarter_code DESC LIMIT 4",
            (trdar_cd,),
        ).fetchall()
        if rows:
            trend = " → ".join(f"{r['quarter_code']}:{r['total']:,}" for r in reversed(rows))
            out.append({
                "text": f"상권 분기 유동인구 추이: {trend}.",
                "ref": f"floating:{trdar_cd}",
                "kind": "floating_trend",
            })
        rows = conn.execute(
            "SELECT quarter_code, SUM(sales_amount) AS s FROM commercial_estimated_sales "
            "WHERE trdar_cd=? GROUP BY quarter_code ORDER BY quarter_code DESC LIMIT 4",
            (trdar_cd,),
        ).fetchall()
        if rows:
            trend = " → ".join(f"{r['quarter_code']}:{r['s']:,}원" for r in reversed(rows))
            out.append({
                "text": f"상권 분기 추정매출 추이: {trend}.",
                "ref": f"sales:{trdar_cd}",
                "kind": "sales_trend",
            })

    # ★ 배후수요: 반경 N m 안 아파트 단지 세대수 (이전 핀트 오류 → 이제 컨텍스트로 정확히 활용)
    cur = conn.execute(
        "SELECT canonical_name, household_count, latitude, longitude, completion_date "
        "FROM apartment_complex WHERE latitude IS NOT NULL"
    ).fetchall()
    if cur:
        nearby = []
        for c in cur:
            d = _haversine_m(ctx.latitude, ctx.longitude, c["latitude"], c["longitude"])
            if d <= ctx.nearby_radius_m:
                nearby.append((d, c))
        if nearby:
            total_hh = sum(c["household_count"] or 0 for _, c in nearby)
            avg_yr = sum(int((c["completion_date"] or "0")[:4]) for _, c in nearby if c["completion_date"]) / max(1, len([1 for _, c in nearby if c["completion_date"]]))
            top3 = ", ".join(
                f"{c['canonical_name']}({c['household_count']}세대,{d:.0f}m)"
                for d, c in sorted(nearby, key=lambda x: x[0])[:3]
            )
            out.append({
                "text": (
                    f"반경 {ctx.nearby_radius_m}m 배후수요: 아파트 {len(nearby)}단지, "
                    f"합산 {total_hh:,}세대, 평균 준공 {int(avg_yr)}년. 대표: {top3}."
                ),
                "ref": f"backing_demand:{ctx.asset_id}",
                "kind": "backing_demand",
            })

    # 행정동 상주 인구
    # admin_dong은 commercial_area를 통해 adstrd_cd 매핑
    if trdar_cd:
        cur = conn.execute(
            "SELECT adstrd_cd, adstrd_name FROM commercial_area WHERE trdar_cd=?",
            (trdar_cd,),
        ).fetchone()
        if cur and cur["adstrd_cd"]:
            res = conn.execute(
                "SELECT quarter_code, total FROM admin_dong_resident_population "
                "WHERE adstrd_cd=? ORDER BY quarter_code DESC LIMIT 1",
                (cur["adstrd_cd"],),
            ).fetchone()
            if res:
                out.append({
                    "text": (
                        f"{cur['adstrd_name']} 상주 인구 {res['quarter_code']} 기준 "
                        f"{res['total']:,}명."
                    ),
                    "ref": f"resident:{cur['adstrd_cd']}",
                    "kind": "resident_pop",
                })

    # 가장 가까운 지하철역
    if _table_exists(conn, "transit_station"):
        stations = conn.execute(
            "SELECT station_name, line_name, latitude, longitude FROM transit_station "
            "WHERE ABS(latitude - ?) < 0.02 AND ABS(longitude - ?) < 0.02",
            (ctx.latitude, ctx.longitude),
        ).fetchall()
        if stations:
            with_dist = sorted(
                stations,
                key=lambda s: _haversine_m(ctx.latitude, ctx.longitude,
                                           s["latitude"], s["longitude"]),
            )
            top = with_dist[0]
            d = _haversine_m(ctx.latitude, ctx.longitude, top["latitude"], top["longitude"])
            out.append({
                "text": (
                    f"가장 가까운 지하철역: {top['station_name']} ({top['line_name']}) "
                    f"— {d:.0f}m."
                ),
                "ref": f"transit:{top['station_name']}",
                "kind": "transit_subway",
            })

    # 가장 가까운 버스정류장 수
    if _table_exists(conn, "bus_stop"):
        bus_stops = conn.execute(
            "SELECT stop_name, latitude, longitude FROM bus_stop "
            "WHERE ABS(latitude - ?) < 0.005 AND ABS(longitude - ?) < 0.005",
            (ctx.latitude, ctx.longitude),
        ).fetchall()
        within_300 = 0
        within_500 = 0
        for s in bus_stops:
            d = _haversine_m(ctx.latitude, ctx.longitude, s["latitude"], s["longitude"])
            if d <= 300: within_300 += 1
            if d <= 500: within_500 += 1
        if within_500:
            out.append({
                "text": f"본 위치 반경 300m 버스정류장 {within_300}개, 500m 내 {within_500}개.",
                "ref": f"bus_stops:{ctx.asset_id}",
                "kind": "bus_stops",
            })

    # 자치구 정비사업
    if ctx.sigungu:
        rows = conn.execute(
            "SELECT category, stage, COUNT(*) AS n FROM development_project "
            "WHERE district=? GROUP BY category, stage ORDER BY n DESC LIMIT 5",
            (ctx.sigungu,),
        ).fetchall()
        for r in rows:
            out.append({
                "text": (
                    f"{ctx.sigungu} {r['category']} "
                    f"{r['stage'] or '미상'}: {r['n']}건."
                ),
                "ref": f"dev:{ctx.sigungu}:{r['category']}:{r['stage']}",
                "kind": "dev_project",
            })

    # MOLIT 상가 매매 통계 (PR-13)
    if _table_exists(conn, "commercial_sale_transaction") and ctx.sigungu_cd:
        r = conn.execute(
            "SELECT COUNT(*) AS n, AVG(deal_amount) AS avg_amt, MAX(deal_year_month) AS latest "
            "FROM commercial_sale_transaction WHERE lawd_cd=?",
            (ctx.sigungu_cd,),
        ).fetchone()
        if r and r["n"]:
            out.append({
                "text": (
                    f"{ctx.sigungu} 상업·업무용 매매 실거래 {r['n']}건, "
                    f"평균 {r['avg_amt']:,.0f}만원, 최근 {r['latest']}."
                ),
                "ref": f"commercial_sale:{ctx.sigungu_cd}",
                "kind": "commercial_sale_stats",
            })

    return out


def _nearest_trdar(conn, lat: float, lng: float) -> str | None:
    """bbox 매칭 우선, 실패 시 중심점 거리 최소."""
    cand = conn.execute(
        "SELECT trdar_cd, center_lat, center_lon FROM commercial_area "
        "WHERE bbox_min_lat<=? AND bbox_max_lat>=? AND bbox_min_lon<=? AND bbox_max_lon>=?",
        (lat, lat, lng, lng),
    ).fetchall()
    if not cand:
        cand = conn.execute(
            "SELECT trdar_cd, center_lat, center_lon FROM commercial_area "
            "WHERE center_lat IS NOT NULL"
        ).fetchall()
        if cand:
            cand = sorted(cand, key=lambda r: _haversine_m(
                lat, lng, r["center_lat"], r["center_lon"]))[:1]
    return cand[0]["trdar_cd"] if cand else None


# ───────────────────────────────────────────────────────────
# Retriever 본체
# ───────────────────────────────────────────────────────────
class SqliteRetriever(Retriever):
    def __init__(self, db_path: str, asset_context: AssetContext):
        self._db_path = db_path
        self._ctx = asset_context
        conn = tracing_connect(db_path, source="sqlite_retriever")
        try:
            self._by_axis: dict[Axis, list[dict]] = {
                "tenant": _candidates_tenant(conn, asset_context),
                "esg": _candidates_esg(conn, asset_context),
                "commercial": _candidates_commercial(conn, asset_context),
            }
        finally:
            conn.close()
        for axis, items in self._by_axis.items():
            logger.info("SqliteRetriever %s: %d candidates", axis, len(items))
        # trace event
        try:
            from real_estate.report.tracing import emit
            emit("retriever_init",
                 candidates_per_axis={a: len(items) for a, items in self._by_axis.items()},
                 asset_id=asset_context.asset_id,
                 bd_mgt_sn=asset_context.bd_mgt_sn,
                 sigungu_cd=asset_context.sigungu_cd,
                 nearby_radius_m=asset_context.nearby_radius_m)
        except Exception:
            pass
        self._vecs_by_axis: dict[Axis, list[list[float]]] = {}
        for axis, items in self._by_axis.items():
            if items:
                self._vecs_by_axis[axis] = embed_texts([it["text"] for it in items])
            else:
                self._vecs_by_axis[axis] = []

    def retrieve(
        self,
        query: str,
        *,
        k: int,
        axis: Axis | None = None,
    ) -> list[GroundedSnippet]:
        qv = embed_one(query)
        scope_axes = [axis] if axis else ["tenant", "esg", "commercial"]
        scored: list[tuple[float, dict, Axis]] = []
        for a in scope_axes:
            items = self._by_axis.get(a, [])
            vecs = self._vecs_by_axis.get(a, [])
            for i, vec in enumerate(vecs):
                score = sum(x * y for x, y in zip(qv, vec))
                scored.append((score, items[i], a))
        scored.sort(key=lambda x: -x[0])
        out: list[GroundedSnippet] = []
        for score, item, a in scored[:k]:
            out.append(GroundedSnippet(
                text=item["text"],
                node_refs=[item["ref"]],
                score=float(score),
                metadata={"axis": a, "kind": item["kind"]},
            ))
        emit_tool_call(
            tool=f"retriever:sqlite:{axis or 'all'}",
            args={"query": query, "k": k, "axis": axis},
            result={"returned": len(out),
                    "snippets": [
                        {"text": s.text, "node_refs": s.node_refs,
                         "score": s.score, "metadata": s.metadata}
                        for s in out
                    ]},
        )
        return out
