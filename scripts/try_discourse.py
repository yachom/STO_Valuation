"""담론 큐레이션 시험 진입점 (STORM PR-3 — 관점 발견 + Q&A 담론 루프).

    python -m scripts.try_discourse "서울 강서구 마곡동 774-1"
    python -m scripts.try_discourse "..." --perspectives 1 --turns 1

흐름: 스코프 시드 + 건물유형 분류 → Perspectivist(관점 발견) → 관점별 Questioner↔
Researcher 다턴 → Moderator 공백 스윕. 그래프에 Fact를 적재하므로(쓰기) + 다중 LLM
콜이라 비용이 든다 — workflow flag와 무관하게 직접 run_discourse_expand를 호출.

필요 env: KAKAO/JUSO/NAVER/ANTHROPIC/OPENAI(researcher=gpt_mini)/NEO4J
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
_DEFAULT_ADDRESS = "서울특별시 강서구 마곡동 774-1"


def _classify_building_type(driver, asset_id):
    """기존 그래프(Building main_use + Tenant 카테고리)로 건물유형 분류 — 재수집 없음."""
    from real_estate.repository.building_type import classify_building_type
    with driver.session() as s:
        b = s.run("MATCH (:Asset {asset_id:$a})-[:ANCHORS]->(b:Building) "
                  "RETURN b.main_use AS mu LIMIT 1", a=asset_id).single()
        rows = s.run("MATCH (:Asset {asset_id:$a})-[:HAS_TENANT]->(t:Tenant) "
                     "RETURN t.카테고리 AS 카테고리", a=asset_id).data()
    return classify_building_type((b or {}).get("mu"), rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="graphflow 담론 큐레이션 (STORM PR-3)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("--perspectives", type=int, default=None, help="발견 관점 상한")
    ap.add_argument("--turns", type=int, default=None, help="관점당 Q&A 턴")
    ap.add_argument("--no-seed", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    load_env()
    for key in ("KAKAO_API_KEY", "JUSO_API_KEY", "ANTHROPIC_API_KEY"):
        if not os.environ.get(key):
            print(f"ERROR: .env에 {key} 필요", file=sys.stderr)
            return 2

    from real_estate.graphflow.discourse import run_discourse_expand
    from infra.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.seeder import asset_id_for, collect_scope, seed
    from infra.agent.tracing import make_run_id, run_tracer
    from real_estate.location import LocationResolver

    location = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"]).resolve(args.address)
    asset_id = asset_id_for(location)

    driver = get_neo4j_driver()
    try:
        run_id = make_run_id("dsc")
        with run_tracer(run_id, Path(DB_PATH), session_id=asset_id,
                        trace_name=f"discourse:{args.address[:30]}",
                        tags=["discourse", "storm"], metadata={"asset_id": asset_id}):
            if not args.no_seed:
                seed(collect_scope(location, DB_PATH), driver)
            btype = _classify_building_type(driver, asset_id)
            print(f"건물유형: {btype.get('유형')} (앵커전략: {btype.get('앵커전략')})")

            result = run_discourse_expand(
                driver, asset_id=asset_id, building_type=btype, collected_by=run_id,
                perspectives_max=args.perspectives, turns_per_perspective=args.turns)

        print(f"\n{'='*70}\n담론 큐레이션 — {args.address}\n{'='*70}")
        print(f"발견 관점({len(result['perspectives_discovered'])}): "
              f"{', '.join(result['perspectives_discovered']) or '(없음)'}")
        print(f"\n관점별 스레드 (3축 척추 + 발견):")
        for t in result["threads"]:
            print(f"  [{t['axis']}] {t['label']} — {t['turns']}턴")
        outline = result.get("outline", {})
        if outline:
            print(f"\n아웃라인 (담론→논점, writer 입력):")
            for ax, pts in outline.items():
                print(f"  [{ax}]")
                for p in pts:
                    print(f"    · {p}")
        print(f"\n담론 라운드(사이클): {result.get('rounds_run', 0)}")
        print("최종 커버리지 (facts/press/uncited/stale):")
        for ax, c in result.get("final_coverage", {}).items():
            print(f"  [{ax}] facts={c['facts']} press={c['press']} "
                  f"uncited={c['uncited']} stale={c['stale']}")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
