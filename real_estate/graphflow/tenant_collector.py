"""상가 입점업체(테넌트) 수집 — 별도 단계 + staleness 갱신.

EXPAND와 같은 분리 원칙: 수집은 따로(느린 카카오 로스터 + Playwright 리뷰
스크래핑), 보고서는 그래프만 읽는다. 동일 자산 재실행 시 collected_at이
staleness_days(기본 90일) 이내면 재수집을 건너뛴다 — 테넌트는 자주 안 바뀌고
스크래핑이 비싸기 때문.

흐름:
    로스터(KakaoLocal 공식 API) → 리뷰/평점(panel3 Playwright) → 앵커 스코어링
    → Tenant 노드 + (Asset)-HAS_TENANT->(Tenant) 적재 (card_text 자동).

Asset 노드는 ScopeGraph에 넣지 않는다 — seed가 card_text를 덮어쓰므로. 대신
기존 Asset을 MATCH하는 HAS_TENANT 엣지만 추가한다.
"""

from __future__ import annotations

import logging
from datetime import date

from infra.agent.tracing import emit, node_scope
from infra.external.kakao_local import KakaoLocalClient
from infra.external.kakao_place_scraper import KakaoPlaceScraper
from infra.external.reb_rone import RebRoneClient
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.seeder import ScopeGraph, SeedEdge, seed
from real_estate.repository.building_tenants import BuildingTenantsRepository
from real_estate.repository.tenant_anchor import score_tenants

logger = logging.getLogger("graphflow.tenant")

DEFAULT_STALENESS_DAYS = 90


def _last_collected(driver, asset_id: str) -> date | None:
    with driver.session() as s:
        r = s.run("MATCH (:Asset {asset_id:$a})-[:HAS_TENANT]->(t:Tenant) "
                  "RETURN max(t.collected_at) AS m", a=asset_id).single()
    m = r["m"] if r else None
    try:
        return date.fromisoformat(m) if m else None
    except (ValueError, TypeError):
        return None


def _scope_top_industries(driver, asset_id: str) -> list[str]:
    """자산 상권의 주력 업종명 (앵커 스코어링의 상권 교차참조용)."""
    with driver.session() as s:
        rows = s.run(
            "MATCH (:Asset {asset_id:$a})-[*1..2]-(:CommercialArea)"
            "-[:TOP_INDUSTRY]->(i:Industry) RETURN DISTINCT i.industry_name AS n",
            a=asset_id).data()
    return [r["n"] for r in rows if r.get("n")]


def _add_market_context(sg, driver, asset_id: str, reb_key: str,
                        region: str = "서울") -> bool:
    """REB 지역 임대시장 환경을 MarketContext 노드로 그래프에 추가 (graceful).

    건물 자체 값이 아니라 임차인이 처한 시장 환경(지역 단위). 실패 시 False.
    """
    from real_estate.repository.reb_lease import RebLeaseRepository
    try:
        with driver.session() as s:
            b = s.run("MATCH (:Asset {asset_id:$a})-[:ANCHORS]->(b:Building) "
                      "RETURN b.main_use AS mu LIMIT 1", a=asset_id).single()
        main_use = (b or {}).get("mu") or ""
        bt = "오피스" if "업무" in main_use else "소규모상가"
        repo = RebLeaseRepository(RebRoneClient(reb_key))
        props = {"region": region, "building_type": bt}
        vac = repo.market("공실률", bt, region=region)
        if vac:
            props["공실률"] = vac["값"]
            props["공실추세"] = vac["추세_pct"]
            props["기준분기"] = vac.get("기준분기설명") or vac.get("기준분기")
        rent = repo.market("임대료", bt, region=region)
        if rent:
            props["임대료"] = rent["값"]
            props.setdefault("기준분기", rent.get("기준분기설명") or rent.get("기준분기"))
        yld = repo.market("수익률", bt, region=region)
        if yld:
            props["수익률"] = yld["값"]
        if not any(k in props for k in ("공실률", "임대료", "수익률")):
            return False
        node = sg.add_node(L.MARKET_CONTEXT, f"reb:{region}:{bt}", props)
        if node is not None:
            sg.edges.append(SeedEdge(
                src_label=L.ASSET, src_key=asset_id, rel=E.HAS_MARKET,
                dst_label=L.MARKET_CONTEXT, dst_key=f"reb:{region}:{bt}"))
        emit("market_context", region=region, building_type=bt,
             공실률=props.get("공실률"))
        return True
    except Exception as e:
        logger.warning("REB 시장환경 수집 실패 (skip): %s", str(e)[:160])
        return False


def collect_tenants(driver, asset_id: str, *, location, kakao_key: str,
                    reb_key: str | None = None,
                    building_name: str | None = None,
                    staleness_days: int = DEFAULT_STALENESS_DAYS,
                    scrape_reviews: bool = True, force: bool = False,
                    today: date | None = None) -> dict:
    """건물 입점업체를 수집·스코어링해 그래프에 적재한다 (staleness 갱신).

    Args:
        driver:      Neo4j 드라이버.
        asset_id:    대상 자산.
        location:    LocationInfo (road_address·좌표).
        kakao_key:   Kakao REST 키.
        building_name: 건물명 (키워드 검색 보강).
        staleness_days: 이 일수 이내 collected_at이면 재수집 skip.
        scrape_reviews: False면 로스터만(리뷰 없이) 빠르게.
        force:       staleness 무시하고 강제 재수집.

    Returns:
        {"skipped": bool, "count": int, "anchors": int, "collected_at": str, ...}
    """
    today = today or date.today()

    if not force:
        last = _last_collected(driver, asset_id)
        if last and (today - last).days < staleness_days:
            emit("tenant_collect_skip", reason="fresh", collected_at=str(last))
            return {"skipped": True, "reason": "fresh", "collected_at": str(last)}

    with node_scope("tenant_collect"):
        repo = BuildingTenantsRepository(KakaoLocalClient(kakao_key))
        tenants = repo.fetch(road_address=location.road_address,
                             x=location.longitude, y=location.latitude,
                             building_name=building_name)

        if scrape_reviews and tenants:
            revs = {r.place_id: r.as_dict()
                    for r in KakaoPlaceScraper().fetch_reviews(
                        [t["place_id"] for t in tenants])}
            for t in tenants:
                t["리뷰"] = revs.get(t["place_id"], {})

        top_inds = _scope_top_industries(driver, asset_id)
        ranked = score_tenants(tenants, top_industries=top_inds)

        coll = str(today)
        sg = ScopeGraph()
        for t in ranked:
            rv = t.get("리뷰") or {}
            tn = sg.add_node(L.TENANT, t["place_id"], {
                "상호": t.get("상호"),
                "카테고리": t.get("카테고리"),
                "카테고리그룹": t.get("카테고리그룹"),
                "도로명주소": t.get("도로명주소"),
                "평점": rv.get("평점"),
                "후기수": rv.get("후기수"),
                "블로그리뷰수": rv.get("블로그리뷰수"),
                "앵커점수": t.get("앵커점수"),
                "앵커등급": t.get("앵커등급"),
                "collected_at": coll,
            })
            if tn is not None:
                # Asset은 노드로 안 넣음(card 덮어쓰기 방지). 기존 Asset을 MATCH하는
                # 엣지만 추가 — _write_edges는 양끝을 MATCH/MERGE만 한다.
                sg.edges.append(SeedEdge(
                    src_label=L.ASSET, src_key=asset_id, rel=E.HAS_TENANT,
                    dst_label=L.TENANT, dst_key=t["place_id"]))

        if reb_key:
            _add_market_context(sg, driver, asset_id, reb_key)
        stats = seed(sg, driver, embed=True)
        anchors = sum(1 for t in ranked if t.get("앵커등급") == "앵커")
        emit("tenant_collect_done", count=len(ranked), anchors=anchors,
             collected_at=coll)
        return {"skipped": False, "count": len(ranked), "anchors": anchors,
                "collected_at": coll, "seed_stats": stats}
