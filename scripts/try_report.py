"""보고서 생성 시험 진입점 — LangGraph 워크플로우 경유 (PR-D/E).

    # 기존 그래프로 보고서만 (EXPAND 생략)
    python -m scripts.try_report "서울특별시 강남구 강남대로 396"

    # 한 바퀴 단일 명령: 시드 → 확장 3라운드 → 보고서
    python -m scripts.try_report "..." --expand-rounds 3

필요 env: KAKAO/JUSO/ANTHROPIC/NEO4J (+확장 시 NAVER, +LANGFUSE 선택)
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
    ap = argparse.ArgumentParser(description="graphflow 보고서 생성 (LangGraph)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("--expand-rounds", type=int, default=0,
                    help="보고서 전 EXPAND 라운드 수 (0=생략)")
    ap.add_argument("--expand-budget", type=int, default=40)
    ap.add_argument("-k", type=int, default=12, help="섹션당 evidence 수")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    load_env()
    needed = ["KAKAO_API_KEY", "JUSO_API_KEY", "ANTHROPIC_API_KEY"]
    if args.expand_rounds > 0:
        needed += ["NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]
    for key in needed:
        if not os.environ.get(key):
            print(f"ERROR: .env에 {key} 필요", file=sys.stderr)
            return 2

    from real_estate.graphflow.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.seeder import asset_id_for
    from infra.agent.tracing import make_run_id, run_tracer
    from real_estate.graphflow.workflow import run_report_pipeline
    from real_estate.location import LocationResolver

    location = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"]).resolve(args.address)
    asset_id = asset_id_for(location)
    print(f"asset_id={asset_id} | expand_rounds={args.expand_rounds}")

    driver = get_neo4j_driver()
    try:
        with run_tracer(make_run_id("rpt"), Path(DB_PATH),
                        session_id=asset_id, trace_name=f"report:{args.address}",
                        tags=["report"],
                        metadata={"asset_id": asset_id,
                                  "expand_rounds": args.expand_rounds}) as tracer:
            final = run_report_pipeline(
                driver, asset_id=asset_id, out_dir=str(tracer.run_dir),
                db_path=DB_PATH, location=location,
                expand_rounds=args.expand_rounds,
                expand_budget=args.expand_budget, evidence_k=args.k)
            out_dir = tracer.run_dir

        report = final["report"]
        exp = final.get("expand_summary", {})
        print(f"\n{'='*70}\n보고서 생성 완료: {report['report_id']}\n{'='*70}")
        if not exp.get("skipped"):
            print(f"  EXPAND: {exp.get('rounds_run')}라운드, "
                  f"검색 {exp.get('queries_total')}건")
        print(f"  섹션 {len(report['sections'])} / 주장 {len(report['claims'])} "
              f"/ 인용 근거 {len(report['evidence'])}")
        print(f"  기계층 잔여 위반: "
              f"{ {k: len(v) for k, v in report['audit']['mechanical'].items()} }")
        llm = report["audit"]["llm"]
        print(f"  LLM층: overall_pass={llm['overall_pass']}, 이슈 {len(llm['issues'])}건")
        for iss in llm["issues"]:
            print(f"    - [{iss['severity']}/{iss['kind']}] {iss['location']}: "
                  f"{iss['description'][:100]}")
        print(f"\n산출물: {out_dir}/report.md, report.json")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
