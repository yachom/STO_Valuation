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
from infra.external.kakao_place_scraper import KakaoPlaceScraper
from infra.external.localdata.client import LocalDataClient
from infra.external.reb_rone import RebRoneClient
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.seeder import ScopeGraph, SeedEdge, seed
from real_estate.graphflow.tool.building_tenants import BuildingTenantsTool
from real_estate.repository.building_turnover import BuildingTurnoverRepository
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


def _fetch_turnover(localdata_key: str, road_address: str | None):
    """LocalData 단일 조회 → (영업중_인허가, 집계dict). 실패/없음이면 (None, None).

    한 번의 조회로 앵커 스코어링(개별 면적·입점일)과 TurnoverStat 노드(집계)를
    모두 채운다 — 업종 API가 늘면 이중 호출 비용이 커지므로 단일 fetch가 중요.
    """
    if not (localdata_key and road_address):
        return None, None
    try:
        repo = BuildingTurnoverRepository(LocalDataClient(localdata_key))
        lic, by_service, skipped = repo.licenses(road_address)
    except Exception as e:
        logger.warning("회전율 수집 실패 (skip): %s", str(e)[:160])
        return None, None
    if not lic:
        emit("turnover_skip", road=road_address, reason="no_data")
        return None, None
    agg = repo.summarize(lic, by_service, skipped, road_address=road_address)
    active = [x for x in lic if not x.is_closed]
    return active, agg


def _add_turnover(sg, asset_id: str, st: dict, *, today: date) -> bool:
    """회전율 집계(st)를 TurnoverStat 노드로 그래프에 추가 (graceful).

    REB(지역 시장환경)와 달리 **이 건물 자체**의 폐업이력 기반 임차 변동.
    nested(dict)는 Neo4j 적재 불가라 문자열화한다.
    """
    if not st or st.get("입점이력", 0) <= 0:
        return False
    road_address = st.get("도로명주소") or ""
    turnover_id = f"turnover:{road_address}"
    dist = st.get("업태분포") or {}
    props = {
        "도로명주소": st.get("도로명주소") or road_address,
        "입점이력": st.get("입점이력"), "영업중": st.get("영업중"),
        "폐업": st.get("폐업"), "누적폐업률": st.get("누적폐업률"),
        "평균존속연수": st.get("평균존속연수"),
        "중앙존속연수": st.get("중앙존속연수"),
        "영업중_평균영업연수": st.get("영업중_평균영업연수"),
        "최근2년_폐업": st.get("최근2년_폐업"),
        "최근2년_신규": st.get("최근2년_신규"),
        # dict/list nested는 Neo4j 프로퍼티 불가 → 스칼라화
        "업태분포": ", ".join(f"{k} {v}" for k, v in dist.items()) or None,
        "수집업종": st.get("수집업종") or None,
        "미수집업종": st.get("미수집업종") or None,
        # 시간속성 미부여 — MarketContext처럼 적재 시점 latest로 교체되는 구조
        # 통계라 validity는 VALID(무감쇠). 재수집은 tenant staleness 게이트가 담당.
        "collected_at": str(today),
    }
    node = sg.add_node(L.TURNOVER_STAT, turnover_id, props)
    if node is not None:
        sg.edges.append(SeedEdge(
            src_label=L.ASSET, src_key=asset_id, rel=E.HAS_TURNOVER,
            dst_label=L.TURNOVER_STAT, dst_key=turnover_id))
    emit("turnover_stat", road=road_address, 입점이력=st.get("입점이력"),
         누적폐업률=st.get("누적폐업률"), 평균존속연수=st.get("평균존속연수"))
    return True


def collect_tenants(driver, asset_id: str, *, location, kakao_key: str,
                    reb_key: str | None = None,
                    localdata_key: str | None = None,
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
        reb_key:     REB R-ONE 키 (있으면 MarketContext 지역 시장환경 추가).
        localdata_key: data.go.kr 키 (있으면 TurnoverStat 건물 회전율 추가).
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
        roster = BuildingTenantsTool(kakao_key)(
            location=location, building_name=building_name)
        tenants = roster.get("입점업체", [])

        if scrape_reviews and tenants:
            revs = {r.place_id: r.as_dict()
                    for r in KakaoPlaceScraper().fetch_reviews(
                        [t["place_id"] for t in tenants])}
            for t in tenants:
                t["리뷰"] = revs.get(t["place_id"], {})

        # 인허가 단일 조회 — 앵커 스코어링(면적·존속)과 TurnoverStat 노드 공용.
        active_lic, turnover_agg = _fetch_turnover(localdata_key, location.road_address)

        top_inds = _scope_top_industries(driver, asset_id)
        ranked = score_tenants(tenants, top_industries=top_inds,
                               licenses=active_lic, today=today)

        coll = str(today)
        sg = ScopeGraph()
        for t in ranked:
            rv = t.get("리뷰") or {}
            sig = t.get("신호") or {}
            tn = sg.add_node(L.TENANT, t["place_id"], {
                "상호": t.get("상호"),
                "카테고리": t.get("카테고리"),
                "카테고리그룹": t.get("카테고리그룹"),
                "도로명주소": t.get("도로명주소"),
                "평점": rv.get("평점"),
                "후기수": rv.get("후기수"),
                "블로그리뷰수": rv.get("블로그리뷰수"),
                # 인허가 결합 신호 (매칭된 가게만 — None은 add_node가 필터)
                "면적": sig.get("면적"),
                "영업연수": sig.get("영업연수"),
                "인허가매칭": sig.get("인허가매칭"),
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
        if turnover_agg:
            _add_turnover(sg, asset_id, turnover_agg, today=today)
        stats = seed(sg, driver, embed=True)
        anchors = sum(1 for t in ranked if t.get("앵커등급") == "앵커")
        emit("tenant_collect_done", count=len(ranked), anchors=anchors,
             collected_at=coll)
        return {"skipped": False, "count": len(ranked), "anchors": anchors,
                "collected_at": coll, "seed_stats": stats}
