"""발행자 피드백 합의 루프 시험 (PR-F).

가장 최근 보고서에 피드백을 주입해 합의 프로토콜을 검증한다. 발행자 역할을
스크립트가 한다 (UI 대체).

    python -m scripts.try_revise --scenario edit       # 표현 수정 → 수용 → block 재작성
    python -m scripts.try_revise --scenario withdraw    # 평가 후 철회 → 보고서 불변
    python -m scripts.try_revise --scenario challenge   # 사실 이의 → 미니 EXPAND → 재작성
    python -m scripts.try_revise --scenario enrich       # 보강 → 미니 EXPAND → 새 block
    python -m scripts.try_revise --scenario attach        # 첨부 자료 → UserDoc → 평가 반영
    python -m scripts.try_revise --scenario rebut          # 재반박 → 새 라운드 → 보고서 불변

필요 env: KAKAO/JUSO/NAVER/ANTHROPIC/NEO4J
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
    return next((d for d in dirs if (d / "report.json").is_file()), None)


def _pick_factual(report: dict, axis: str = "commercial") -> tuple[str, str]:
    for cid, cl in report["claims"].items():
        if cl["axis"] == axis and cl["kind"] == "factual":
            return cid, cl["text"]
    cid = next(iter(report["claims"]))
    return cid, report["claims"][cid]["text"]


def _blocks(report: dict) -> dict[str, str]:
    return {b["block_id"]: b["markdown"]
            for sec in report["sections"] for b in sec["blocks"]}


# 시나리오별 피드백 정의
_SCENARIOS = {
    "edit": dict(kind="edit",
                 comment="이 문단은 수치 나열이 많아 읽기 어렵다. 핵심 결론을 앞세우고 근거 수치는 보조로 풀어 써 달라.",
                 decision="accept"),
    "withdraw": dict(kind="edit",
                     comment="표현을 더 단정적으로 바꿔 달라.", decision="withdraw"),
    "challenge": dict(kind="challenge",
                      comment="공실·임대시장 관련 서술의 근거가 오래됐을 수 있다. 최신 강남역 상권 공실 상황을 확인해 반영하거나, 근거가 약하면 단정을 완화해 달라.",
                      decision="accept"),
    "enrich": dict(kind="enrich",
                   comment="이 섹션에 강남역 일대 최근 개발/교통 호재 정보가 부족하다. 최신 동향을 보강해 달라.",
                   decision="accept"),
    "attach": dict(kind="challenge",
                   comment="첨부한 관리사무소 자료 기준으로 현재 공실 상황을 반영해 달라.",
                   decision="accept",
                   attachments=[{"provider": "건물 관리사무소 제공",
                                 "text": "2026년 6월 기준 본 강남역 지하도상가의 점포 공실률은 6.5%이며, "
                                         "최근 6개월간 신규 임차 3건이 체결되어 공실이 감소 추세에 있다."}]),
    "rebut": dict(kind="edit",
                  comment="이 문단이 너무 길다. 줄여 달라.", decision="rebut",
                  rebut_comment="앞 지적에 더해, 행정동 인구 수치는 빼고 상권 결론만 남겨 달라."),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="graphflow REVISE 합의 루프 (PR-F)")
    ap.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    ap.add_argument("--scenario", choices=list(_SCENARIOS), default="edit")
    ap.add_argument("--axis", default="commercial")
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
        print("ERROR: 보고서 없음. 먼저 try_report 실행.", file=sys.stderr)
        return 2
    report = json.loads((rpt_dir / "report.json").read_text())
    before = copy.deepcopy(report)
    spec = _SCENARIOS[args.scenario]
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
    target_id, target_text = _pick_factual(report, args.axis)

    driver = get_neo4j_driver()
    try:
        asset_card = _asset_card(driver, asset_id)
        with run_tracer(make_run_id("rev"), Path(DB_PATH),
                        session_id=asset_id, trace_name=f"revise:{spec['kind']}",
                        tags=["revise", spec["kind"]],
                        metadata={"asset_id": asset_id, "scenario": args.scenario,
                                  "report_id": report["report_id"]}):
            print(f"\n[피드백] scenario={args.scenario}, kind={spec['kind']}, target={target_id}")
            print(f"  대상 주장: {target_text[:70]}")
            print(f"  comment: {spec['comment'][:90]}")
            if spec.get("attachments"):
                print(f"  첨부: {len(spec['attachments'])}건 — {spec['attachments'][0]['text'][:60]}…")

            result = submit_feedback(driver, report, asset_id, target_id=target_id,
                                     kind=spec["kind"], comment=spec["comment"],
                                     attachments=spec.get("attachments"))
            print(f"\n[Assessment] stance={result['stance']}")
            print(f"  rationale: {result['rationale'][:200]}")
            if result["proposal"]:
                print(f"  proposal: {result['proposal'][:120]}")
            print(f"\n  평가 단계 보고서 불변: {'✓' if report == before else '✗'}")

            out = resolve_feedback(
                driver, report, asset_id, feedback_id=result["feedback_id"],
                target_claim_id=result["_target_claim_id"], kind=spec["kind"],
                comment=spec["comment"], decision=spec["decision"],
                out_dir=str(rpt_dir), asset_card=asset_card,
                rebut_comment=spec.get("rebut_comment"))

            print(f"\n[결정: {spec['decision']}] status={out['status']}, "
                  f"보고서 변경={out['report_changed']}")
            if spec["decision"] == "rebut":
                print(f"  새 라운드 {out['round']}: stance={out['stance']}")
                print(f"  rationale: {out['rationale'][:150]}")
                print(f"  rebut 후 보고서 불변: {'✓' if report == before else '✗'}")
            elif out["report_changed"]:
                after = out["report"]
                changed = [b for b in _blocks(before)
                           if _blocks(before)[b] != _blocks(after).get(b)]
                added = [b for b in _blocks(after) if b not in _blocks(before)]
                print(f"  새 버전: {out['version']} (parent={after.get('parent_version_id')})")
                print(f"  변경 block: {changed}, 추가 block: {added}")
                if spec["kind"] == "enrich":
                    print(f"  기존 block 전부 불변: "
                          f"{'✓' if not changed else '✗ ' + str(changed)}")
                else:
                    other = [b for b in _blocks(before) if b not in changed]
                    intact = all(_blocks(before)[b] == _blocks(after).get(b) for b in other)
                    print(f"  타깃 외 {len(other)}개 block 불변: {'✓' if intact else '✗'}")
            else:
                print(f"  보고서 불변 확인: {'✓' if report == before else '✗'}")
        return 0
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
