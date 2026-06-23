"""Moderator 시험 진입점 (STORM PR-2 — 커버리지 GAP→미니EXPAND 재진입).

    python -m scripts.try_moderator "서울 강서구 마곡동 774-1"
    python -m scripts.try_moderator "..." --max-rounds 1 --no-seed

흐름: 스코프 시드(idempotent) → 초기 커버리지 측정 → Moderator 루프
(coverage→질문→미니EXPAND) → 최종 커버리지. workflow의 flag와 무관하게 직접
run_moderated_expand를 호출해 모더레이터를 항상 구동한다(시험용).

필요 env: KAKAO/JUSO/NAVER/ANTHROPIC/NEO4J
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


def _print_coverage(title, cov) -> None:
    from real_estate.graphflow.coverage import coverage_summary_text
    print(f"\n=== {title} ===")
    print(coverage_summary_text(cov))


def main() -> int:
    ap = argparse.ArgumentParser(description="graphflow Moderator (STORM PR-2)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("--max-rounds", type=int, default=None,
                    help="moderator 라운드 캡 (기본 config.curation)")
    ap.add_argument("--no-seed", action="store_true", help="스코프 시드 건너뜀")
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

    from real_estate.graphflow.coverage import axis_coverage
    from infra.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.moderator import run_moderated_expand
    from real_estate.graphflow.seeder import asset_id_for, collect_scope, seed
    from infra.agent.tracing import make_run_id, run_tracer
    from real_estate.location import LocationResolver

    location = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"]).resolve(args.address)
    asset_id = asset_id_for(location)

    driver = get_neo4j_driver()
    try:
        run_id = make_run_id("mod")
        with run_tracer(run_id, Path(DB_PATH), session_id=asset_id,
                        trace_name=f"moderator:{args.address[:30]}",
                        tags=["moderator", "storm"], metadata={"asset_id": asset_id}):
            if not args.no_seed:
                seed(collect_scope(location, DB_PATH), driver)

            _print_coverage("초기 커버리지", axis_coverage(driver, asset_id))

            result = run_moderated_expand(
                driver, asset_id=asset_id, collected_by=run_id,
                max_rounds=args.max_rounds)

        print(f"\n{'='*70}\nMODERATOR 루프 — {args.address}\n{'='*70}")
        print(f"라운드 실행: {result['rounds_run']}")
        for h in result["history"]:
            if h.get("done"):
                print(f"  R{h['round']}: 종료 — {h.get('reason', '')}")
            else:
                print(f"  R{h['round']}: 질문 {h['questions']}개 "
                      f"(축: {', '.join(h.get('axes', []))})")
        print("\n최종 커버리지 (facts/press/uncited/stale):")
        for ax, c in result["final_coverage"].items():
            print(f"  [{ax}] facts={c['facts']} press={c['press']} "
                  f"uncited={c['uncited']} stale={c['stale']}")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
