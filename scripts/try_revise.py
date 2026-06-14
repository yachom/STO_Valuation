"""발행자 피드백 합의 루프 시험 (PR-F).

가장 최근 생성된 보고서(reports/rpt_*/report.json)에 피드백을 주입해 합의
프로토콜을 검증한다. 발행자 역할을 스크립트가 한다 (UI 대체).

    # edit 피드백 → 평가 → 수용 → block 재작성
    python -m scripts.try_revise "서울특별시 강남구 강남대로 396" --scenario edit

    # disagree/withdraw → 보고서 불변 확인
    python -m scripts.try_revise "..." --scenario withdraw

필요 env: KAKAO/JUSO/ANTHROPIC/NEO4J
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
from pathlib import Path

from infra.env import load_env

DB_PATH = str(Path(__file__).resolve().parent.parent
              / "real_estate" / "database" / "DB" / "real_estate.db")
_DEFAULT_ADDRESS = "서울특별시 강남구 강남대로 396"


def _latest_report_dir() -> Path | None:
    base = Path(DB_PATH).parent / "reports"
    dirs = sorted(base.glob("rpt_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs:
        if (d / "report.json").is_file():
            return d
    return None


def _pick_target(report: dict, axis: str = "commercial") -> tuple[str, str]:
    """검증용 타깃 — 지정 축의 첫 factual claim (block 단위 edit 대상)."""
    for cid, cl in report["claims"].items():
        if cl["axis"] == axis and cl["kind"] == "factual":
            return cid, cl["text"]
    cid = next(iter(report["claims"]))
    return cid, report["claims"][cid]["text"]


def main() -> int:
    ap = argparse.ArgumentParser(description="graphflow REVISE 합의 루프 (PR-F)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("--scenario", choices=["edit", "withdraw"], default="edit")
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

    rpt_dir = _latest_report_dir()
    if rpt_dir is None:
        print("ERROR: 보고서가 없습니다. 먼저 try_report로 생성하세요.", file=sys.stderr)
        return 2
    report = json.loads((rpt_dir / "report.json").read_text())
    report_before = copy.deepcopy(report)
    print(f"대상 보고서: {report['report_id']} ({report['version_id']}) @ {rpt_dir.name}")

    from real_estate.graphflow.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.report import _asset_card
    from real_estate.graphflow.reviser import resolve_feedback, submit_feedback
    from real_estate.graphflow.seeder import asset_id_for
    from infra.agent.tracing import make_run_id, run_tracer
    from real_estate.location import LocationResolver

    location = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"]).resolve(args.address)
    asset_id = asset_id_for(location)

    target_id, target_text = _pick_target(report)
    comment = ("이 문단은 수치 나열이 많아 읽기 어렵다. 핵심 결론을 앞세우고 "
               "근거 수치는 보조로 풀어 써 달라.")

    driver = get_neo4j_driver()
    try:
        asset_card = _asset_card(driver, asset_id)
        with run_tracer(make_run_id("rev"), Path(DB_PATH)):
            print(f"\n[피드백] target={target_id}")
            print(f"  대상 주장: {target_text[:70]}")
            print(f"  kind=edit, comment={comment}")

            result = submit_feedback(driver, report, asset_id,
                                     target_id=target_id, kind="edit", comment=comment)
            print(f"\n[Assessment 회신] stance={result['stance']}")
            print(f"  rationale: {result['rationale']}")
            print(f"  proposal: {result['proposal']}")

            # 합의 전 보고서 불변 확인
            unchanged = report == report_before
            print(f"\n  평가 단계 보고서 불변: {'✓' if unchanged else '✗ 변경됨!'}")

            if args.scenario == "withdraw":
                out = resolve_feedback(
                    driver, report, asset_id, feedback_id=result["feedback_id"],
                    target_claim_id=result["_target_claim_id"], kind="edit",
                    comment=comment, decision="withdraw", out_dir=str(rpt_dir),
                    asset_card=asset_card)
                print(f"\n[발행자 철회] status={out['status']}, "
                      f"보고서 변경={out['report_changed']}")
                print(f"  철회 후 보고서 불변: "
                      f"{'✓' if report == report_before else '✗'}")
            else:
                out = resolve_feedback(
                    driver, report, asset_id, feedback_id=result["feedback_id"],
                    target_claim_id=result["_target_claim_id"], kind="edit",
                    comment=comment, decision="accept", out_dir=str(rpt_dir),
                    asset_card=asset_card)
                report = out["report"]
                print(f"\n[발행자 수용] status={out['status']}, "
                      f"새 버전={out.get('version')}")
                # 타깃 외 불변 검증
                _verify_surgical(report_before, report, target_id)

        return 0
    finally:
        close_neo4j_driver()


def _verify_surgical(before: dict, after: dict, target_id: str) -> None:
    """타깃 block만 바뀌고 나머지는 불변인지 검증."""
    # 타깃 block_id 찾기
    tgt_block = before["claims"].get(target_id, {}).get("block_id")
    before_blocks = {b["block_id"]: b["markdown"]
                     for sec in before["sections"] for b in sec["blocks"]}
    after_blocks = {b["block_id"]: b["markdown"]
                    for sec in after["sections"] for b in sec["blocks"]}
    changed = [bid for bid in before_blocks
               if before_blocks.get(bid) != after_blocks.get(bid)]
    print(f"\n  변경된 block: {changed}")
    print(f"  타깃 block({tgt_block})만 변경: "
          f"{'✓' if changed == [tgt_block] else '✗ (' + str(changed) + ')'}")
    other_unchanged = all(
        before_blocks[bid] == after_blocks.get(bid)
        for bid in before_blocks if bid != tgt_block)
    print(f"  타깃 외 {len(before_blocks)-1}개 block 불변: "
          f"{'✓' if other_unchanged else '✗'}")
    print(f"  버전 체인: {before['version_id']} → {after['version_id']} "
          f"(parent={after.get('parent_version_id')})")


if __name__ == "__main__":
    raise SystemExit(main())
