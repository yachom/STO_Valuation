"""그래프 정비 시험 (PR-G, Janitor).

    python -m scripts.try_janitor --dry-run    # 탐지만 (변경 없음)
    python -m scripts.try_janitor               # 정비 실행

필요 env: NEO4J
"""

from __future__ import annotations

import argparse
import logging

from infra.env import load_env


def _entity_snapshot(driver) -> dict:
    with driver.session() as s:
        live = s.run("MATCH (e:Entity) WHERE e.merged_into IS NULL "
                     "RETURN count(*) AS n").single()["n"]
        merged = s.run("MATCH (e:Entity) WHERE e.merged_into IS NOT NULL "
                       "RETURN count(*) AS n").single()["n"]
        review = s.run("MATCH (e:Entity) WHERE e.needs_review = true "
                       "RETURN count(*) AS n").single()["n"]
        # 강남역 CommercialArea에 직접 MENTIONS 거는 Fact 수 (흡수 효과 측정)
        gangnam = s.run(
            "MATCH (f:Fact)-[:MENTIONS]->(t:CommercialArea {trdar_name:'강남역'}) "
            "RETURN count(DISTINCT f) AS n").single()["n"]
        return {"live": live, "merged": merged, "review": review,
                "gangnam_direct_facts": gangnam}


def main() -> int:
    ap = argparse.ArgumentParser(description="graphflow Janitor (PR-G)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    load_env()

    from infra.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.janitor import run_janitor

    driver = get_neo4j_driver()
    try:
        before = _entity_snapshot(driver)
        print(f"정비 전: live Entity {before['live']}, superseded {before['merged']}, "
              f"리뷰 {before['review']}, 강남역 직접 Fact {before['gangnam_direct_facts']}")

        result = run_janitor(driver, auto=not args.dry_run, dry_run=args.dry_run)

        if args.dry_run:
            print(f"\n{'='*66}\n[dry-run] 탐지 결과\n{'='*66}")
            print(f"  승격 후보: {result['promotions']}건")
            for x in result.get("promotion_samples", []):
                print(f"    · {x}")
            print(f"  병합 후보: {result['duplicates']}건")
            for x in result.get("duplicate_samples", []):
                print(f"    · {x}")
            print(f"  오병합(리뷰 큐): {result['conflations']}건")
            for x in result.get("conflation_samples", []):
                print(f"    · {x}")
            return 0

        print(f"\n{'='*66}\n정비 결과\n{'='*66}")
        print(f"  승격 {result['promotions']}, 병합 {result['duplicates']}, "
              f"오병합 리뷰 {result['conflations']}")
        print(f"  Fact 이관 {result['facts_moved']}건, "
              f"Entity superseded {result['entities_superseded']}, "
              f"리뷰 큐 {result['review_queued']}")
        after = _entity_snapshot(driver)
        print(f"\n정비 후: live Entity {after['live']} "
              f"(−{before['live']-after['live']}), superseded {after['merged']}, "
              f"리뷰 {after['review']}")
        print(f"  강남역 CommercialArea 직접 Fact: "
              f"{before['gangnam_direct_facts']} → {after['gangnam_direct_facts']} "
              f"(+{after['gangnam_direct_facts']-before['gangnam_direct_facts']} 흡수)")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
