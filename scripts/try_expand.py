"""EXPAND 루프 시험 진입점 (PR-C).

주소 → 시드 → 멀티 홉 확장 (Navigator/Harvester/Curator) → 결과 눈검사.

    # 1라운드만, 검색 예산 작게 (검색어 품질 게이트용)
    python -m scripts.try_expand "서울특별시 강남구 강남대로 396" --rounds 1 --budget 8

    # 풀 예산 (라운드 3, 총 40검색)
    python -m scripts.try_expand "서울특별시 강남구 강남대로 396"

필요 env: KAKAO/JUSO/NAVER/ANTHROPIC/NEO4J (+LANGFUSE 선택)
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

_DEFAULT_ADDRESS = "서울특별시 강남구 강남대로 396"


def main() -> int:
    ap = argparse.ArgumentParser(description="graphflow EXPAND 시험 (PR-C)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--frontier", type=int, default=8)
    ap.add_argument("--budget", type=int, default=40, help="총 검색 쿼리 예산")
    ap.add_argument("--no-trace", action="store_true", help="로컬 사이드카 비활성")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    load_env()
    for key in ("KAKAO_API_KEY", "JUSO_API_KEY", "NAVER_CLIENT_ID",
                "NAVER_CLIENT_SECRET", "ANTHROPIC_API_KEY"):
        if not os.environ.get(key):
            print(f"ERROR: .env에 {key} 필요", file=sys.stderr)
            return 2

    from real_estate.graphflow.expand import run_expand
    from real_estate.graphflow.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.graph.schema import NODE_LABELS as L
    from real_estate.graphflow.seeder import asset_id_for, collect_scope, seed
    from real_estate.graphflow.tracing import make_run_id, run_tracer
    from real_estate.location import LocationResolver

    print(f"주소 정규화: {args.address}")
    location = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"]).resolve(args.address)
    asset_id = asset_id_for(location)
    print(f"  asset_id={asset_id}")

    driver = get_neo4j_driver()
    run_id = make_run_id("exp")
    try:
        print("시드 적재...")
        seed(collect_scope(location, DB_PATH), driver)

        if args.no_trace:
            summary = run_expand(driver, asset_id=asset_id, collected_by=run_id,
                                 rounds=args.rounds, frontier_max=args.frontier,
                                 total_query_budget=args.budget)
        else:
            with run_tracer(run_id, Path(DB_PATH)) as tracer:
                summary = run_expand(driver, asset_id=asset_id, collected_by=run_id,
                                     rounds=args.rounds, frontier_max=args.frontier,
                                     total_query_budget=args.budget)
                print(f"\ntrace: {tracer.run_dir}")

        print(f"\n{'='*70}\nEXPAND 요약\n{'='*70}")
        for row in summary["history"]:
            print(f"  round {row['round']}: 타깃 {row['targets']}, 검색 {row['queries']}, "
                  f"신규 문서 {row['docs_new']}, Fact {row['facts_new']}, "
                  f"Entity {row['entities_new']}, 확정연결 {row['mentions_confirmed']}, "
                  f"후보연결 {row['mentions_candidate']}, novelty {row['novelty']}")

        # ── 검증 출력: 실행된 검색어 / 수집 Fact와 연결 ──
        with driver.session() as s:
            print("\n── 실행된 검색어 (provenance) ──")
            for r in s.run(
                f"MATCH (src)-[:SPAWNED_QUERY]->(q:{L.QUERY}) "
                f"WHERE q.collected_by = $cb "
                f"RETURN labels(src)[0] AS lbl, q.text AS text, q.n_results AS n "
                f"ORDER BY lbl, text", cb=run_id):
                print(f"  [{r['lbl']}] {r['text']}  (결과 {r['n']}건)")

            print("\n── 수집된 Fact (연결 포함, 최신 10건) ──")
            for r in s.run(
                f"MATCH (f:{L.FACT}) WHERE f.collected_by = $cb "
                f"OPTIONAL MATCH (f)-[:MENTIONS]->(m) "
                f"WITH f, collect(labels(m)[0] + ':' + coalesce(m.trdar_name, m.adstrd_name, "
                f"m.industry_name, m.name, m.canonical_name, '?')) AS mentions "
                f"RETURN f.text AS text, f.confidence AS conf, f.valid_until AS vu, "
                f"f.temporal_kind AS tk, mentions LIMIT 10", cb=run_id):
                vu = f", valid_until={r['vu']}" if r["vu"] else ""
                print(f"  · {r['text']}")
                print(f"    (conf={r['conf']}, {r['tk']}{vu}) → {r['mentions']}")

            print("\n── 그래프 도달성: Asset에서 Fact까지 경로 존재? ──")
            r = s.run(
                f"MATCH (a:{L.ASSET} {{asset_id: $aid}})-[*1..4]-(f:{L.FACT}) "
                f"RETURN count(DISTINCT f) AS n", aid=asset_id).single()
            print(f"  Asset 4홉 내 도달 가능한 Fact: {r['n']}건")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
