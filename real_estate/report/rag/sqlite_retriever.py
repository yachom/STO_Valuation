"""SQLite-only Retriever — Neo4j 없이도 동작하는 우회 구현.

운영(Neo4j) 환경 미구성 시 fallback. 같은 Retriever Protocol을 만족하므로
에이전트는 변화 없이 동작.

전략:
    1. real_estate.db에서 단지/인증/상권 등 축별 관련 row를 SQL로 추출
    2. 각 row → 한국어 facts text 변환 (template-based)
    3. bge-m3로 query 임베딩 → row 텍스트 임베딩 cosine 정렬
    4. top-k GroundedSnippet 반환

asset_context (lat/lng/legal_address)가 있으면 그것으로 좁힌 검색을 한다.
없으면 axis 일반 stats만 반환.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass

from real_estate.report.rag.embedder import embed_one, embed_texts
from real_estate.report.rag.protocol import Axis, GroundedSnippet, Retriever

logger = logging.getLogger("rag.sqlite_retriever")


@dataclass
class AssetContext:
    """retriever가 자산 위치/식별을 알아야 axis 안에서 의미 있는 후보를 모은다."""
    asset_id: str
    latitude: float | None = None
    longitude: float | None = None
    legal_address: str | None = None
    road_address: str | None = None
    master_complex_id: int | None = None
    sigungu: str | None = None     # 자치구명 ('강남구')


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ───────────────────────────────────────────────────────────
# axis별 candidate 수집
# ───────────────────────────────────────────────────────────
def _candidates_tenant(conn, ctx: AssetContext) -> list[dict]:
    out: list[dict] = []
    if ctx.master_complex_id is not None:
        r = conn.execute(
            "SELECT * FROM apartment_complex WHERE master_complex_id=?",
            (ctx.master_complex_id,),
        ).fetchone()
        if r:
            addr = r["road_address"] or r["legal_address"] or ""
            out.append({
                "text": (
                    f"본 단지 {r['canonical_name']} ({addr}): "
                    f"{r['completion_date'] or '준공일미상'} 준공, "
                    f"{r['household_count'] or '?'}세대, "
                    f"{r['total_buildings'] or '?'}동, data_source={r['data_source']}."
                ),
                "ref": f"apartment_complex:{r['master_complex_id']}",
                "kind": "complex_meta",
            })
    if ctx.sigungu:
        for r in conn.execute(
            "SELECT canonical_name, household_count, completion_date "
            "FROM apartment_complex WHERE road_address LIKE ? OR legal_address LIKE ? "
            "ORDER BY household_count DESC LIMIT 5",
            (f"%{ctx.sigungu}%", f"%{ctx.sigungu}%"),
        ):
            out.append({
                "text": (
                    f"동일 자치구 대단지 — {r['canonical_name']}: "
                    f"{r['household_count'] or '?'}세대, "
                    f"{r['completion_date'] or '?'} 준공."
                ),
                "ref": f"region_complex:{ctx.sigungu}",
                "kind": "region_peer",
            })
    # 매매 거래 (있을 경우)
    try:
        if ctx.master_complex_id is not None:
            r = conn.execute(
                "SELECT COUNT(*) AS n, AVG(deal_amount) AS avg_amt, "
                "MAX(deal_year_month) AS latest FROM apt_sale_transaction "
                "WHERE master_complex_id=?",
                (ctx.master_complex_id,),
            ).fetchone()
            if r and r["n"]:
                out.append({
                    "text": f"본 단지 매매 실거래 {r['n']}건, 평균 {r['avg_amt']:,.0f}만원, "
                            f"최근 {r['latest']}.",
                    "ref": f"apt_sale:{ctx.master_complex_id}",
                    "kind": "sale_stats",
                })
    except sqlite3.OperationalError:
        pass
    try:
        if ctx.master_complex_id is not None:
            for r in conn.execute(
                "SELECT contract_type, COUNT(*) AS n, AVG(deposit) AS avg_dep, "
                "AVG(monthly_rent) AS avg_rent FROM apt_rent_transaction "
                "WHERE master_complex_id=? GROUP BY contract_type",
                (ctx.master_complex_id,),
            ):
                out.append({
                    "text": f"본 단지 {r['contract_type']} {r['n']}건, "
                            f"평균 보증금 {(r['avg_dep'] or 0):,.0f}만원, "
                            f"평균 월세 {(r['avg_rent'] or 0):,.0f}만원.",
                    "ref": f"apt_rent:{ctx.master_complex_id}",
                    "kind": "rent_stats",
                })
    except sqlite3.OperationalError:
        pass
    try:
        if ctx.sigungu:
            for r in conn.execute(
                "SELECT lease_type, COUNT(*) AS n, SUM(household_count) AS hh "
                "FROM public_lease_complex WHERE region_name LIKE ? "
                "GROUP BY lease_type ORDER BY n DESC LIMIT 5",
                (f"%{ctx.sigungu}%",),
            ):
                out.append({
                    "text": f"동일 자치구 {r['lease_type']} 공공임대 {r['n']}개 단지, "
                            f"합산 {r['hh'] or 0:,}세대.",
                    "ref": f"public_lease:{ctx.sigungu}",
                    "kind": "lh_stats",
                })
    except sqlite3.OperationalError:
        pass
    return out


def _candidates_esg(conn, ctx: AssetContext) -> list[dict]:
    out: list[dict] = []
    if not ctx.sigungu:
        return out
    # BEEC 등급 분포
    for r in conn.execute(
        "SELECT grd_name, COUNT(*) AS n FROM energy_efficiency_cert "
        "WHERE jibun_addr LIKE ? OR full_addr LIKE ? "
        "GROUP BY grd_name ORDER BY n DESC LIMIT 10",
        (f"%{ctx.sigungu}%", f"%{ctx.sigungu}%"),
    ):
        out.append({
            "text": f"{ctx.sigungu} BEEC 에너지효율 인증 {r['grd_name']} 등급 — {r['n']}건.",
            "ref": f"beec:{ctx.sigungu}:{r['grd_name']}",
            "kind": "beec",
        })
    # G-SEED 등급 분포
    for r in conn.execute(
        "SELECT cert_grade, cert_type, COUNT(*) AS n FROM green_cert "
        "WHERE jibun_addr LIKE ? OR address LIKE ? "
        "GROUP BY cert_grade, cert_type ORDER BY n DESC LIMIT 10",
        (f"%{ctx.sigungu}%", f"%{ctx.sigungu}%"),
    ):
        out.append({
            "text": f"{ctx.sigungu} G-SEED 녹색건축 {r['cert_grade']}({r['cert_type']}) — {r['n']}건.",
            "ref": f"gseed:{ctx.sigungu}:{r['cert_grade']}",
            "kind": "gseed",
        })
    # 본 단지가 직접 인증 받았는지 (단지명 substring 매칭)
    if ctx.master_complex_id is not None:
        target = conn.execute(
            "SELECT canonical_name FROM apartment_complex WHERE master_complex_id=?",
            (ctx.master_complex_id,),
        ).fetchone()
        if target:
            name = target["canonical_name"]
            hit_b = conn.execute(
                "SELECT COUNT(*) AS n FROM energy_efficiency_cert WHERE bld_nm LIKE ?",
                (f"%{name}%",),
            ).fetchone()
            if hit_b and hit_b["n"]:
                out.append({
                    "text": f"본 단지({name})와 이름이 일치하는 BEEC 인증 건물 {hit_b['n']}건 발견 (단지 직접 인증 가능성).",
                    "ref": f"beec_self:{name}",
                    "kind": "beec_self",
                })
            hit_g = conn.execute(
                "SELECT COUNT(*) AS n FROM green_cert WHERE building_name LIKE ?",
                (f"%{name}%",),
            ).fetchone()
            if hit_g and hit_g["n"]:
                out.append({
                    "text": f"본 단지({name})와 이름이 일치하는 G-SEED 인증 건물 {hit_g['n']}건 발견.",
                    "ref": f"gseed_self:{name}",
                    "kind": "gseed_self",
                })
    return out


def _candidates_commercial(conn, ctx: AssetContext) -> list[dict]:
    out: list[dict] = []
    if not ctx.latitude or not ctx.longitude:
        return out
    # bbox 매칭
    cand = conn.execute(
        "SELECT trdar_cd, trdar_name, trdar_se_name, area_sqm, center_lat, center_lon "
        "FROM commercial_area "
        "WHERE bbox_min_lat <= ? AND bbox_max_lat >= ? "
        "AND bbox_min_lon <= ? AND bbox_max_lon >= ?",
        (ctx.latitude, ctx.latitude, ctx.longitude, ctx.longitude),
    ).fetchall()
    if not cand:
        cand = conn.execute(
            "SELECT trdar_cd, trdar_name, trdar_se_name, area_sqm, center_lat, center_lon "
            "FROM commercial_area WHERE center_lat IS NOT NULL"
        ).fetchall()
        if cand:
            cand = sorted(cand, key=lambda r: _haversine_m(
                ctx.latitude, ctx.longitude, r["center_lat"], r["center_lon"]))[:1]
    if not cand:
        return out
    area = cand[0]
    distance = _haversine_m(ctx.latitude, ctx.longitude, area["center_lat"], area["center_lon"])
    out.append({
        "text": f"인접 상권 '{area['trdar_name']}' ({area['trdar_se_name']}), "
                f"면적 {area['area_sqm']:,.0f}㎡, 중심거리 {distance:.0f}m.",
        "ref": f"trdar:{area['trdar_cd']}",
        "kind": "commercial_area",
    })
    trdar_cd = area["trdar_cd"]
    # 최근 4 분기 유동인구 시계열
    rows = conn.execute(
        "SELECT quarter_code, total FROM commercial_floating_population "
        "WHERE trdar_cd=? ORDER BY quarter_code DESC LIMIT 4",
        (trdar_cd,),
    ).fetchall()
    if len(rows) >= 1:
        latest = rows[0]
        trend = " → ".join(f"{r['quarter_code']}: {r['total']:,}" for r in reversed(rows))
        out.append({
            "text": f"{area['trdar_name']} 분기 유동인구 추이 — {trend}.",
            "ref": f"floating:{trdar_cd}",
            "kind": "floating_trend",
        })
    # 최근 4 분기 매출 합
    rows = conn.execute(
        "SELECT quarter_code, SUM(sales_amount) AS s FROM commercial_estimated_sales "
        "WHERE trdar_cd=? GROUP BY quarter_code ORDER BY quarter_code DESC LIMIT 4",
        (trdar_cd,),
    ).fetchall()
    if rows:
        trend = " → ".join(f"{r['quarter_code']}: {r['s']:,}원" for r in reversed(rows))
        out.append({
            "text": f"{area['trdar_name']} 분기 추정매출 추이 — {trend}.",
            "ref": f"sales:{trdar_cd}",
            "kind": "sales_trend",
        })
    # 업종별 top 5
    rows = conn.execute(
        "SELECT industry_name, SUM(sales_amount) AS s FROM commercial_estimated_sales "
        "WHERE trdar_cd=? GROUP BY industry_code ORDER BY s DESC LIMIT 5",
        (trdar_cd,),
    ).fetchall()
    if rows:
        top = ", ".join(f"{r['industry_name']}({r['s']:,}원)" for r in rows)
        out.append({
            "text": f"{area['trdar_name']} 상위 업종 매출(누적) — {top}.",
            "ref": f"industry:{trdar_cd}",
            "kind": "industry",
        })
    # 자치구 정비사업
    if ctx.sigungu:
        cnt = conn.execute(
            "SELECT category, stage, COUNT(*) AS n FROM development_project "
            "WHERE district=? GROUP BY category, stage ORDER BY n DESC LIMIT 8",
            (ctx.sigungu,),
        ).fetchall()
        for r in cnt:
            out.append({
                "text": f"{ctx.sigungu} {r['category']} 정비사업 {r['stage'] or '미상'} 단계 — {r['n']}건.",
                "ref": f"dev:{ctx.sigungu}:{r['category']}:{r['stage']}",
                "kind": "dev_project",
            })
    return out


# ───────────────────────────────────────────────────────────
# Retriever 본체
# ───────────────────────────────────────────────────────────
class SqliteRetriever(Retriever):
    def __init__(self, db_path: str, asset_context: AssetContext):
        self._db_path = db_path
        self._ctx = asset_context
        # axis별 후보를 미리 한 번 다 수집해서 그 안에서 cosine 정렬.
        # 데이터 변경이 거의 없는 read-only 평가 시나리오라 1회 캐시 안전.
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._by_axis: dict[Axis, list[dict]] = {
                "tenant": _candidates_tenant(conn, asset_context),
                "esg": _candidates_esg(conn, asset_context),
                "commercial": _candidates_commercial(conn, asset_context),
            }
        for axis, items in self._by_axis.items():
            logger.info("SqliteRetriever %s: %d candidates", axis, len(items))
        # bge-m3 임베딩 (배치)
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
        if axis is not None:
            scope_axes = [axis]
        else:
            scope_axes = ["tenant", "esg", "commercial"]

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
        return out
