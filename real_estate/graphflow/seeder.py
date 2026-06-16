"""Seeder — 주소 1건 → 자산 스코프 그래프 적재 (설계 §4.4, PR-A).

전체 SQLite 일괄 적재가 아니라 **Asset 앵커에서 닿는 것만** 스코프 적재한다.
구 scout 12종의 SQL을 그래프 적재 쿼리로 흡수 — "에이전트가 매번 SQL 도구를
부르는" 패턴이 "그래프에 미리 구워두고 순회하는" 패턴으로 바뀐다.

2단 구조 (Neo4j 없이도 그래프 모양 검증 가능):
    1. collect_scope(location, db_path) -> ScopeGraph   # 순수 SQLite, 오프라인
    2. seed(scope, driver, embed=...)    -> dict         # Neo4j MERGE 적재

결정적·idempotent. 같은 자산 재평가 시 MERGE로 갱신만 일어난다.

이번 스코프 (PR-A):
    Asset, Building, LegalDong, AdminDong, CommercialArea, Industry,
    Certification(BEEC/GSEED), Complex(배후수요), TransitStation.
deferred (후속): DevelopmentProject·commercial_sale 노드화, 건축HUB 대장 보강,
    REB/NTS 시군구 시장 요약(현재 DB 0행).
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field

from neo4j import Driver

from real_estate.graphflow.graph.cards import build_card
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import KEY_PROP
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.location import LocationInfo

logger = logging.getLogger("graphflow.seeder")

_NEARBY_RADIUS_M = 800     # 배후수요 반경
_NEARBY_LIMIT = 8          # 인접 단지 최대 노드 수
_TOP_INDUSTRY_N = 5        # 상권 매출 상위 업종 수


# ───────────────────────────────────────────────────────────
# 스코프 그래프 (직렬화 가능한 중간 표현)
# ───────────────────────────────────────────────────────────
@dataclass
class SeedNode:
    label: str
    key: str                       # 키 프로퍼티 값
    props: dict = field(default_factory=dict)
    card_text: str = ""


@dataclass
class SeedEdge:
    src_label: str
    src_key: str
    rel: str
    dst_label: str
    dst_key: str
    props: dict = field(default_factory=dict)


@dataclass
class ScopeGraph:
    nodes: list[SeedNode] = field(default_factory=list)
    edges: list[SeedEdge] = field(default_factory=list)
    _index: dict = field(default_factory=dict, repr=False)  # (label,key)->SeedNode

    def add_node(self, label: str, key, props: dict) -> SeedNode | None:
        """노드 추가 (dedup). key None이면 skip. 같은 (label,key)는 props 병합."""
        if key is None or key == "":
            return None
        key = str(key)
        clean = {k: v for k, v in props.items() if v is not None and v != ""}
        clean[KEY_PROP[label]] = key
        existing = self._index.get((label, key))
        if existing is not None:
            existing.props.update(clean)
            existing.card_text = build_card(label, existing.props)
            return existing
        node = SeedNode(label=label, key=key, props=clean,
                        card_text=build_card(label, clean))
        self.nodes.append(node)
        self._index[(label, key)] = node
        return node

    def add_edge(self, src: SeedNode | None, rel: str, dst: SeedNode | None, props: dict | None = None) -> None:
        if src is None or dst is None:
            return
        self.edges.append(SeedEdge(
            src_label=src.label, src_key=src.key, rel=rel,
            dst_label=dst.label, dst_key=dst.key,
            props={k: v for k, v in (props or {}).items() if v is not None},
        ))

    def summary(self) -> dict:
        by_label: dict[str, int] = {}
        for n in self.nodes:
            by_label[n.label] = by_label.get(n.label, 0) + 1
        by_rel: dict[str, int] = {}
        for e in self.edges:
            by_rel[e.rel] = by_rel.get(e.rel, 0) + 1
        return {"nodes": len(self.nodes), "edges": len(self.edges),
                "by_label": by_label, "by_rel": by_rel}


# ───────────────────────────────────────────────────────────
# 기하 헬퍼
# ───────────────────────────────────────────────────────────
def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _nearest_trdar(conn, lat: float, lng: float) -> str | None:
    """bbox 매칭 우선, 실패 시 중심점 거리 최소 상권."""
    cand = conn.execute(
        "SELECT trdar_cd, center_lat, center_lon FROM commercial_area "
        "WHERE bbox_min_lat<=? AND bbox_max_lat>=? AND bbox_min_lon<=? AND bbox_max_lon>=?",
        (lat, lat, lng, lng),
    ).fetchall()
    if not cand:
        rows = conn.execute(
            "SELECT trdar_cd, center_lat, center_lon FROM commercial_area "
            "WHERE center_lat IS NOT NULL"
        ).fetchall()
        if not rows:
            return None
        cand = sorted(rows, key=lambda r: _haversine_m(lat, lng, r["center_lat"], r["center_lon"]))[:1]
    return cand[0]["trdar_cd"]


# ───────────────────────────────────────────────────────────
# 스코프 수집 (순수 SQLite — 오프라인 검증 가능)
# ───────────────────────────────────────────────────────────
def asset_id_for(location: LocationInfo) -> str:
    """자산 식별자 규칙: 건물관리번호 우선, 없으면 좌표 (asset.py와 동일 규칙)."""
    if location.bd_mgt_sn:
        return f"re:{location.bd_mgt_sn}"
    return f"re:{location.latitude},{location.longitude}"


def collect_scope(
    location: LocationInfo,
    db_path: str,
    *,
    radius_m: int = _NEARBY_RADIUS_M,
    building_hub_key: str | None = None,
) -> ScopeGraph:
    """LocationInfo + SQLite → 자산 스코프 ScopeGraph.

    building_hub_key: None이면 BUILDING_HUB_API_KEY env 폴백, ""이면 명시적 비활성
    (오프라인 순수 SQLite 경로). 키가 있으면 Building 노드에 건축물대장 표제부
    (연면적·층수·용도·준공·에너지등급)를 보강한다 — 유일한 네트워크 의존.
    """
    if building_hub_key is None:
        import os
        building_hub_key = os.environ.get("BUILDING_HUB_API_KEY", "")
    sg = ScopeGraph()

    bd = location.bd_mgt_sn
    asset_id = asset_id_for(location)
    sigungu_cd = location.bcode[:5] if location.bcode else None

    asset = sg.add_node(L.ASSET, asset_id, {
        "raw_address": location.raw_address,
        "road_address": location.road_address,
        "jibun_address": location.jibun_address,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "bd_mgt_sn": bd,
        "sigungu": location.sigungu,
        "sigungu_cd": sigungu_cd,
        "bcode": location.bcode,
        "bname": location.bname,
    })

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _collect_location_anchors(sg, conn, asset, location, building_hub_key)
        _collect_certifications(sg, conn, asset, location)
        _collect_commercial(sg, conn, asset, location, radius_m)
    finally:
        conn.close()

    logger.info("collect_scope %s → %s", asset_id, sg.summary())
    return sg


def _collect_location_anchors(sg, conn, asset, loc, building_hub_key="") -> None:
    """LegalDong + 자산 자신의 AdminDong + Building 앵커."""
    legal = sg.add_node(L.LEGAL_DONG, loc.bcode, {"bname": loc.bname})
    sg.add_edge(asset, E.ANCHORS, legal)

    # 자산 본인의 행정동 (hcode[:8] = adstrd_cd). 인근 상권의 행정동과 다를 수
    # 있다 — 상권 행정동을 자산에 앵커링하면 "서초2동 소재" 같은 모순이 생긴다.
    if loc.hcode:
        own_dong = _collect_admin_dong(sg, conn, loc.hcode[:8], loc.hname)
        sg.add_edge(asset, E.ANCHORS, own_dong)

    if loc.bd_mgt_sn:
        # 건물 메타는 cert 테이블에서 best-effort 보강 (없으면 최소 노드)
        meta = conn.execute(
            "SELECT bld_nm AS name, jibun_addr FROM energy_efficiency_cert "
            "WHERE bdmgt_sn=? LIMIT 1", (loc.bd_mgt_sn,),
        ).fetchone()
        if meta is None:
            meta = conn.execute(
                "SELECT building_name AS name, jibun_addr FROM green_cert "
                "WHERE bdmgt_sn=? LIMIT 1", (loc.bd_mgt_sn,),
            ).fetchone()
        bprops = {"name": meta["name"] if meta else None,
                  "jibun_addr": (meta["jibun_addr"] if meta else None) or loc.jibun_address}
        # 건축HUB 대장 보강 (표제부 → 연면적·층수·용도·준공·에너지등급)
        if building_hub_key:
            bprops.update(_fetch_building_register(loc, building_hub_key))
        building = sg.add_node(L.BUILDING, loc.bd_mgt_sn, bprops)
        sg.add_edge(asset, E.ANCHORS, building)
        sg.add_edge(building, E.LOCATED_IN, legal)


def _fetch_building_register(loc: LocationInfo, key: str) -> dict:
    """건축HUB 표제부 → Building 프로퍼티. 실패는 best-effort (빈 dict)."""
    try:
        from real_estate.graphflow.tool.building_hub import BuildingRegisterTool
        result = BuildingRegisterTool(key)(location=loc)
        return result.get("building_props", {})
    except Exception as exc:
        logger.warning("건축HUB 보강 실패 (계속 진행): %s", exc)
        return {}


def _collect_certifications(sg, conn, asset, loc) -> None:
    """본 건물 직접 BEEC/GSEED 인증 (bd_mgt_sn 매칭) → Building -CERTIFIED_AS-> Cert."""
    if not loc.bd_mgt_sn:
        return
    building = sg._index.get((L.BUILDING, loc.bd_mgt_sn))
    if building is None:
        return

    for r in conn.execute(
        "SELECT rqid, grd_name, crtif_isu_dd, primary_energy_req, energy_reduce_rate "
        "FROM energy_efficiency_cert WHERE bdmgt_sn=?", (loc.bd_mgt_sn,),
    ).fetchall():
        cert = sg.add_node(L.CERTIFICATION, f"BEEC:{r['rqid']}", {
            "kind": "BEEC", "grade": r["grd_name"], "issue_date": r["crtif_isu_dd"],
            "primary_energy_req": r["primary_energy_req"],
            "energy_reduce_rate": r["energy_reduce_rate"],
        })
        sg.add_edge(building, E.CERTIFIED_AS, cert)

    for r in conn.execute(
        "SELECT cert_no, cert_grade, cert_date, cert_standard, cert_type "
        "FROM green_cert WHERE bdmgt_sn=?", (loc.bd_mgt_sn,),
    ).fetchall():
        cert = sg.add_node(L.CERTIFICATION, f"GSEED:{r['cert_no']}", {
            "kind": "GSEED", "grade": r["cert_grade"], "issue_date": r["cert_date"],
            "cert_standard": r["cert_standard"], "cert_type": r["cert_type"],
        })
        sg.add_edge(building, E.CERTIFIED_AS, cert)


def _collect_commercial(sg, conn, asset, loc, radius_m) -> None:
    """상권 + 행정동 + 업종 + 배후수요 단지 + 교통."""
    if not (loc.latitude and loc.longitude):
        return

    trdar_cd = _nearest_trdar(conn, loc.latitude, loc.longitude)
    ca_node = None
    if trdar_cd:
        ca_node = _collect_commercial_area(sg, conn, asset, loc, trdar_cd)

    _collect_nearby_complexes(sg, conn, asset, loc, radius_m)
    _collect_transit(sg, conn, asset, loc)


def _benchmark_sales(conn, trdar_cd, se_name, adstrd_cd, latest_q) -> dict:
    """상권 매출의 '동일 자치구·동일 상권구분 내 순위' + 최근 분기 추세.

    비교 grounding — '631억(양호)' 같은 근거 없는 형용사 대신 '강서구 발달상권
    N곳 중 K위(상위 X%), 매출 추세 ±Y%'를 보고서에 공급한다. 이게 있어야
    LLM(특히 상위 모델)이 묘사가 아닌 판정·추론을 할 길이 열린다.
    """
    if not (se_name and adstrd_cd and latest_q):
        return {}
    sgg = str(adstrd_cd)[:5]
    peers = conn.execute(
        "SELECT es.trdar_cd AS cd, SUM(es.sales_amount) AS total "
        "FROM commercial_estimated_sales es "
        "JOIN commercial_area ca ON es.trdar_cd = ca.trdar_cd "
        "WHERE substr(ca.adstrd_cd,1,5)=? AND ca.trdar_se_name=? AND es.quarter_code=? "
        "GROUP BY es.trdar_cd ORDER BY total DESC",
        (sgg, se_name, latest_q),
    ).fetchall()
    out: dict = {}
    rank = next((i + 1 for i, r in enumerate(peers) if str(r["cd"]) == str(trdar_cd)), None)
    if rank:
        out["sales_rank"] = rank
        out["sales_peer_count"] = len(peers)
    trend = list(reversed(conn.execute(
        "SELECT quarter_code AS q, SUM(sales_amount) AS s FROM commercial_estimated_sales "
        "WHERE trdar_cd=? GROUP BY quarter_code ORDER BY quarter_code DESC LIMIT 4",
        (trdar_cd,),
    ).fetchall()))
    if len(trend) >= 2 and trend[0]["s"]:
        out["sales_trend_pct"] = round((trend[-1]["s"] - trend[0]["s"]) / trend[0]["s"] * 100, 1)
        out["sales_trend_span"] = f'{trend[0]["q"]}~{trend[-1]["q"]}'
    return out


def _collect_commercial_area(sg, conn, asset, loc, trdar_cd) -> object | None:
    ca = conn.execute(
        "SELECT trdar_name, trdar_se_name, area_sqm, center_lat, center_lon, "
        "adstrd_cd, adstrd_name FROM commercial_area WHERE trdar_cd=?", (trdar_cd,),
    ).fetchone()
    if ca is None:
        return None

    props = {"trdar_name": ca["trdar_name"], "trdar_se_name": ca["trdar_se_name"],
             "area_sqm": ca["area_sqm"]}
    if ca["center_lat"] and ca["center_lon"]:
        props["distance_m"] = round(_haversine_m(
            loc.latitude, loc.longitude, ca["center_lat"], ca["center_lon"]))

    # 최신 분기 유동인구 요약
    fl = conn.execute(
        "SELECT quarter_code, total FROM commercial_floating_population "
        "WHERE trdar_cd=? ORDER BY quarter_code DESC LIMIT 1", (trdar_cd,),
    ).fetchone()
    if fl:
        props["latest_quarter"] = fl["quarter_code"]
        props["latest_floating_total"] = fl["total"]

    # 최신 분기 매출 요약
    sales = conn.execute(
        "SELECT quarter_code, SUM(sales_amount) AS s FROM commercial_estimated_sales "
        "WHERE trdar_cd=? GROUP BY quarter_code ORDER BY quarter_code DESC LIMIT 1",
        (trdar_cd,),
    ).fetchone()
    latest_sales_q = sales["quarter_code"] if sales else None
    if sales:
        props.setdefault("latest_quarter", sales["quarter_code"])
        props["latest_sales_total"] = sales["s"]
        props.update(_benchmark_sales(
            conn, trdar_cd, ca["trdar_se_name"], ca["adstrd_cd"], latest_sales_q))

    ca_node = sg.add_node(L.COMMERCIAL_AREA, trdar_cd, props)
    sg.add_edge(asset, E.ANCHORS, ca_node, {"distance_m": props.get("distance_m")})

    # 상권의 행정동 — 상권을 통해서만 연결 (자산 ANCHORS 아님: 자산 본인의
    # 행정동은 _collect_location_anchors가 담당)
    if ca["adstrd_cd"]:
        ad_node = _collect_admin_dong(sg, conn, ca["adstrd_cd"], ca["adstrd_name"])
        sg.add_edge(ca_node, E.SERVES_DISTRICT, ad_node)

    # 상위 업종 (최신 분기 매출 상위 N) — Industry는 전역 공유 노드.
    # 분기 필터를 상권 총매출(latest_sales_total)과 동일 분기로 맞춰야
    # '업종 매출 > 상권 총매출' 모순이 안 생긴다 (둘 다 최신 1분기 기준).
    area_total = props.get("latest_sales_total") or 0
    for r in conn.execute(
        "SELECT industry_code, industry_name, SUM(sales_amount) AS s "
        "FROM commercial_estimated_sales WHERE trdar_cd=? AND quarter_code=? "
        "GROUP BY industry_code ORDER BY s DESC LIMIT ?",
        (trdar_cd, latest_sales_q, _TOP_INDUSTRY_N),
    ).fetchall():
        # 매출비중 = 업종 매출 / 상권 총매출 (동일 최신 분기 기준이라 정합).
        share = round(r["s"] / area_total, 4) if area_total else None
        ind = sg.add_node(L.INDUSTRY, r["industry_code"], {
            "industry_name": r["industry_name"],
            "trdar_name": ca["trdar_name"],   # 카드 가독성용 (가장 최근 연결 상권)
            "sales_amount": r["s"],
            "sales_share": share,             # 상권 매출 대비 비중 (0~1)
            "sales_quarter": latest_sales_q,
        })
        sg.add_edge(ca_node, E.TOP_INDUSTRY, ind, {"sales_amount": r["s"]})

    return ca_node


def _collect_admin_dong(sg, conn, adstrd_cd, adstrd_name) -> object:
    props = {"adstrd_name": adstrd_name}
    res = conn.execute(
        "SELECT quarter_code, total FROM admin_dong_resident_population "
        "WHERE adstrd_cd=? ORDER BY quarter_code DESC LIMIT 1", (adstrd_cd,),
    ).fetchone()
    if res:
        props["latest_resident_total"] = res["total"]
        props["latest_resident_quarter"] = res["quarter_code"]
    return sg.add_node(L.ADMIN_DONG, adstrd_cd, props)


def _collect_nearby_complexes(sg, conn, asset, loc, radius_m) -> None:
    """반경 radius_m 안 아파트 단지 (배후수요). 가까운 순 최대 _NEARBY_LIMIT개."""
    rows = conn.execute(
        "SELECT master_complex_id, canonical_name, road_address, household_count, "
        "completion_date, latitude, longitude FROM apartment_complex "
        "WHERE latitude IS NOT NULL",
    ).fetchall()
    nearby = []
    for c in rows:
        d = _haversine_m(loc.latitude, loc.longitude, c["latitude"], c["longitude"])
        if d <= radius_m:
            nearby.append((d, c))
    nearby.sort(key=lambda x: x[0])
    for d, c in nearby[:_NEARBY_LIMIT]:
        node = sg.add_node(L.COMPLEX, c["master_complex_id"], {
            "canonical_name": c["canonical_name"], "road_address": c["road_address"],
            "household_count": c["household_count"], "completion_date": c["completion_date"],
        })
        sg.add_edge(asset, E.WITHIN_RADIUS, node, {"distance_m": round(d)})


def _collect_transit(sg, conn, asset, loc) -> None:
    """가장 가까운 지하철역 1개."""
    if not _table_exists(conn, "transit_station"):
        return
    stations = conn.execute(
        "SELECT station_id, station_name, line_name, latitude, longitude "
        "FROM transit_station WHERE ABS(latitude-?)<0.02 AND ABS(longitude-?)<0.02",
        (loc.latitude, loc.longitude),
    ).fetchall()
    if not stations:
        return
    best = min(stations, key=lambda s: _haversine_m(
        loc.latitude, loc.longitude, s["latitude"], s["longitude"]))
    d = _haversine_m(loc.latitude, loc.longitude, best["latitude"], best["longitude"])
    node = sg.add_node(L.TRANSIT_STATION, best["station_id"], {
        "station_name": best["station_name"], "line_name": best["line_name"],
    })
    sg.add_edge(asset, E.WITHIN_RADIUS, node, {"distance_m": round(d)})


# ───────────────────────────────────────────────────────────
# Neo4j 적재 (제네릭 MERGE writer)
# ───────────────────────────────────────────────────────────
def _write_nodes(driver: Driver, nodes: list[SeedNode]) -> int:
    """라벨별로 묶어 UNWIND MERGE. card_text 포함, props 통째 SET."""
    by_label: dict[str, list[SeedNode]] = {}
    for n in nodes:
        by_label.setdefault(n.label, []).append(n)
    total = 0
    with driver.session() as session:
        for label, items in by_label.items():
            key_prop = KEY_PROP[label]
            rows = [{"key": n.key,
                     "props": {k: v for k, v in n.props.items() if k != key_prop},
                     "card": n.card_text} for n in items]
            session.run(
                f"""
                UNWIND $rows AS row
                MERGE (n:{label} {{{key_prop}: row.key}})
                SET n += row.props, n.card_text = row.card
                """,
                rows=rows,
            )
            total += len(items)
    return total


def _write_edges(driver: Driver, edges: list[SeedEdge]) -> int:
    """(src_label, rel, dst_label)별로 묶어 UNWIND MATCH+MERGE."""
    grouped: dict[tuple, list[SeedEdge]] = {}
    for e in edges:
        grouped.setdefault((e.src_label, e.rel, e.dst_label), []).append(e)
    total = 0
    with driver.session() as session:
        for (src_label, rel, dst_label), items in grouped.items():
            src_key = KEY_PROP[src_label]
            dst_key = KEY_PROP[dst_label]
            rows = [{"src": e.src_key, "dst": e.dst_key, "props": e.props} for e in items]
            session.run(
                f"""
                UNWIND $rows AS row
                MATCH (s:{src_label} {{{src_key}: row.src}})
                MATCH (d:{dst_label} {{{dst_key}: row.dst}})
                MERGE (s)-[r:{rel}]->(d)
                SET r += row.props
                """,
                rows=rows,
            )
            total += len(items)
    return total


def seed(scope: ScopeGraph, driver: Driver, *, embed: bool = False) -> dict:
    """ScopeGraph를 Neo4j에 MERGE 적재. idempotent.

    embed=True면 신규/변경 노드 card_text를 임베딩해 벡터 인덱스까지 채운다
    (모델 ~1.5GB 로드). 기본 False — 빠른 구조 적재만.
    """
    from real_estate.graphflow.graph.schema import apply_constraints

    apply_constraints(driver)
    n_nodes = _write_nodes(driver, scope.nodes)
    n_edges = _write_edges(driver, scope.edges)
    result = {"nodes": n_nodes, "edges": n_edges}

    if embed:
        from real_estate.graphflow.graph.vector_index import (
            ensure_vector_index,
            update_all_embeddings,
        )
        ensure_vector_index(driver)
        result["embedded"] = update_all_embeddings(driver)

    logger.info("seed DONE: %s", result)
    return result


def seed_from_location(
    location: LocationInfo,
    db_path: str,
    driver: Driver,
    *,
    radius_m: int = _NEARBY_RADIUS_M,
    embed: bool = False,
) -> dict:
    """편의 진입점: collect_scope + seed 한 번에."""
    scope = collect_scope(location, db_path, radius_m=radius_m)
    out = seed(scope, driver, embed=embed)
    out["scope"] = scope.summary()
    return out
