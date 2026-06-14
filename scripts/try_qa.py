"""QA 시험 진입점 (PR-E).

    python -m scripts.try_qa "서울특별시 강남구 강남대로 396" "강남역 상권 공실 상황 어때?"
    python -m scripts.try_qa "..." "..." --no-expand    # 라이브 확장 끔

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
_DEFAULT_ADDRESS = "서울특별시 강남구 강남대로 396"


def main() -> int:
    ap = argparse.ArgumentParser(description="graphflow QA (PR-E)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("question", nargs="?", default="강남역 상권 공실 상황 어때?")
    ap.add_argument("--no-expand", action="store_true", help="라이브 확장 비활성")
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
    from real_estate.graphflow.qa import QAResponder
    from real_estate.graphflow.seeder import asset_id_for
    from infra.agent.tracing import make_run_id, run_tracer
    from real_estate.location import LocationResolver

    location = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"]).resolve(args.address)
    asset_id = asset_id_for(location)

    driver = get_neo4j_driver()
    try:
        with run_tracer(make_run_id("qa"), Path(DB_PATH),
                        session_id=asset_id, trace_name=f"qa:{args.question[:30]}",
                        tags=["qa"], metadata={"asset_id": asset_id}):
            result = QAResponder().answer(
                driver, asset_id, args.question,
                allow_expand=not args.no_expand)

        print(f"\n{'='*70}\nQ: {result.question}\n{'='*70}")
        print(f"\n{result.answer_markdown}\n")
        print(f"  상태: sufficient={result.sufficient}, "
              f"라이브확장={'발동' if result.expanded else '안함'}, "
              f"GAP={'기록됨' if result.gap else '없음'}")
        if result.citations:
            print(f"\n  인용 근거 ({len(result.citations)}건):")
            for c in result.citations:
                new = " 🆕신규수집" if c.is_new else ""
                print(f"   - [{c.ref}] ({c.source}, {(c.published_at or '')[:10]}, "
                      f"{c.validity_status}){new}")
                print(f"     {c.text[:100]}")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
