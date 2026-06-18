"""중심 테넌트(앵커) 스코어링 시험 — 카카오 로스터 + LocalData 인허가 결합.

주소 → 위치해소 → 스코프 시드 → collect_tenants(force) → 앵커 순위 출력.
인허가(면적·존속)가 카카오 신호에 어떻게 결합되는지 실데이터로 눈검사한다.

사용:
    python -m scripts.try_tenant_anchor "서울 강서구 마곡동 774-1"
    python -m scripts.try_tenant_anchor --no-reviews "..."   # 리뷰 스크랩 생략(빠름)

필요 env: KAKAO_API_KEY, JUSO_API_KEY, DATA_GO_KR_API_KEY(턴오버),
          REB_LEASE_MARKET_KEY(선택), NEO4J_*.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from infra.env import load_env

DB_PATH = str(Path(__file__).resolve().parent.parent
              / "real_estate" / "database" / "DB" / "real_estate.db")
_DEFAULT_ADDRESS = "서울 강서구 마곡동 774-1"


def main() -> int:
    ap = argparse.ArgumentParser(description="앵커 스코어링(카카오+턴오버) 시험")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("--no-reviews", action="store_true", help="카카오 리뷰 스크랩 생략")
    ap.add_argument("--no-force", action="store_true", help="staleness 게이트 적용")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    load_env()

    from real_estate.location import LocationResolver
    from real_estate.graphflow.seeder import asset_id_for, collect_scope, seed
    from real_estate.graphflow.graph.driver import get_neo4j_driver
    from real_estate.graphflow.tenant_collector import collect_tenants

    resolver = LocationResolver(kakao_api_key=os.environ["KAKAO_API_KEY"],
                                juso_api_key=os.environ.get("JUSO_API_KEY", ""))
    loc = resolver.resolve(args.address)
    asset_id = asset_id_for(loc)
    print(f"\n주소: {args.address}\n  → asset_id={asset_id}")
    print(f"  → road={loc.road_address} / 좌표=({loc.latitude},{loc.longitude})")

    driver = get_neo4j_driver()

    print("\n[1] 스코프 시드 (idempotent MERGE) …")
    scope = collect_scope(loc, DB_PATH)
    seed(scope, driver)
    print(f"  {scope.summary()}")

    # 건물명 (카카오 키워드 검색 보강)
    with driver.session() as s:
        b = s.run("MATCH (:Asset {asset_id:$a})-[:ANCHORS]->(b:Building) "
                  "RETURN b.name AS n LIMIT 1", a=asset_id).single()
    building_name = (b or {}).get("n")

    print("\n[2] collect_tenants (카카오 로스터+리뷰 + LocalData 인허가 결합) …")
    summary = collect_tenants(
        driver, asset_id, location=loc,
        kakao_key=os.environ["KAKAO_API_KEY"],
        reb_key=os.environ.get("REB_LEASE_MARKET_KEY"),
        localdata_key=os.environ.get("DATA_GO_KR_API_KEY"),
        building_name=building_name,
        scrape_reviews=not args.no_reviews,
        force=not args.no_force)
    print(f"  요약: {summary}")

    # 그래프에서 앵커 순위 + 턴오버 노드 출력
    print("\n[3] 앵커 순위 (그래프 HAS_TENANT, 앵커점수 내림차순)")
    with driver.session() as s:
        rows = s.run(
            "MATCH (:Asset {asset_id:$a})-[:HAS_TENANT]->(t:Tenant) "
            "RETURN t.상호 AS 상호, t.카테고리 AS 카테고리, t.앵커점수 AS 점수, "
            "       t.앵커등급 AS 등급, t.면적 AS 면적, t.영업연수 AS 영업연수, "
            "       t.인허가매칭 AS 매칭 ORDER BY t.앵커점수 DESC LIMIT 25", a=asset_id).data()
    print(f"  {'상호':22s} {'점수':>6s} {'등급':>4s} {'면적':>6s} {'영업':>5s} 매칭  카테고리")
    for r in rows:
        nm = (r["상호"] or "")[:20]
        area = f"{r['면적']:.0f}㎡" if r["면적"] else "-"
        yrs = f"{r['영업연수']:.0f}y" if r["영업연수"] else "-"
        mk = "○" if r["매칭"] else "·"
        print(f"  {nm:22s} {r['점수'] or 0:6.3f} {r['등급'] or '-':>4s} {area:>6s} {yrs:>5s}  {mk}  {(r['카테고리'] or '')[:30]}")

    print("\n[4] TurnoverStat 노드 (건물 회전율 집계)")
    with driver.session() as s:
        tn = s.run("MATCH (:Asset {asset_id:$a})-[:HAS_TURNOVER]->(x:TurnoverStat) "
                   "RETURN x.card_text AS c, x.수집업종 AS svc, x.미수집업종 AS skip",
                   a=asset_id).single()
    if tn:
        print(f"  {tn['c']}")
        print(f"  수집업종={tn['svc']} / 미수집(미신청)={tn['skip']}")
    else:
        print("  (TurnoverStat 없음 — DATA_GO_KR_API_KEY 또는 인허가 이력 부재)")

    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
