"""보고서 생성 시험 진입점 (PR-D).

EXPAND가 끝난 그래프에서 보고서 1건 생성:
    python -m scripts.try_report "서울특별시 강남구 강남대로 396"

필요 env: KAKAO/JUSO/ANTHROPIC/NEO4J (+LANGFUSE 선택)
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
    ap = argparse.ArgumentParser(description="graphflow 보고서 생성 (PR-D)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("-k", type=int, default=12, help="섹션당 evidence 수")
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

    from real_estate.graphflow.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.report import generate_report
    from real_estate.graphflow.seeder import asset_id_for
    from real_estate.graphflow.tracing import run_tracer
    from real_estate.location import LocationResolver

    location = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"]).resolve(args.address)
    asset_id = asset_id_for(location)
    print(f"asset_id={asset_id}")

    driver = get_neo4j_driver()
    try:
        from real_estate.graphflow.tracing import make_run_id
        run_id = make_run_id("rpt")
        with run_tracer(run_id, Path(DB_PATH)) as tracer:
            report = generate_report(driver, asset_id,
                                     out_dir=tracer.run_dir, evidence_k=args.k)
            out_dir = tracer.run_dir

        print(f"\n{'='*70}\n보고서 생성 완료: {report['report_id']}\n{'='*70}")
        print(f"  섹션 {len(report['sections'])} / 주장 {len(report['claims'])} "
              f"/ 인용 근거 {len(report['evidence'])}")
        print(f"  기계층 잔여 위반: "
              f"{ {k: len(v) for k, v in report['audit']['mechanical'].items()} }")
        llm = report["audit"]["llm"]
        print(f"  LLM층: overall_pass={llm['overall_pass']}, "
              f"이슈 {len(llm['issues'])}건")
        for iss in llm["issues"]:
            print(f"    - [{iss['severity']}/{iss['kind']}] {iss['location']}: "
                  f"{iss['description']}")
        print(f"\n산출물: {out_dir}/report.md, report.json")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
