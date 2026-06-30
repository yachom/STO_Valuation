"""음악저작권 end-to-end 스모크 테스트 — 전체 라인을 단계별 검증.

resolve → seed → (EXPAND) → report 각 단계를 실행하고 핵심 불변식을 확인해
PASS/FAIL을 출력한다. 음악 전용 Neo4j(MUSIC_NEO4J_*)를 사용한다. 회귀 확인용.

실행 (음악 Neo4j 가동 + .env 키 필요):
    python -m music_copyright.scripts.test_e2e
    python -m music_copyright.scripts.test_e2e --title "좋니" --artist "윤종신"
    python -m music_copyright.scripts.test_e2e --expand          # EXPAND 단계 포함
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

from infra.env import load_env

_results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(cond), label))
    mark = "✅ PASS" if cond else "❌ FAIL"
    print(f"  {mark}  {label}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def main() -> int:
    p = argparse.ArgumentParser(description="음악 end-to-end 스모크 테스트")
    p.add_argument("--title", default="Dynamite")
    p.add_argument("--artist", default="BTS")
    p.add_argument("--expand", action="store_true", help="EXPAND 1라운드 포함")
    args = p.parse_args()

    load_env()
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    ua = os.environ.get("MUSICBRAINZ_USER_AGENT")
    lastfm = os.environ.get("LASTFM_API_KEY")
    if not (cid and secret and ua):
        print("SKIP: SPOTIFY_CLIENT_ID/SECRET, MUSICBRAINZ_USER_AGENT 필요")
        return 2

    print(f"=== e2e: {args.artist} - {args.title} (expand={args.expand}) ===\n")

    # 1) 인식자
    print("[1] resolve")
    from music_copyright.resolver import WorkResolver
    ident = WorkResolver(spotify_client_id=cid, spotify_client_secret=secret,
                         musicbrainz_user_agent=ua).resolve(title=args.title, artist=args.artist)
    check(bool(ident.work_id), "work_id 생성", ident.work_id or "")
    check(bool(ident.iswc or ident.isrc), "ISWC 또는 ISRC 해소",
          f"iswc={ident.iswc} isrc={ident.isrc}")

    from music_copyright.graphflow.graph.driver import close_music_driver, get_music_driver
    driver = get_music_driver()
    try:
        # 2) 시드
        print("[2] seed")
        from music_copyright.graphflow.seeder import collect_scope, seed
        scope = collect_scope(ident, lastfm_api_key=lastfm, mb_user_agent=ua)
        seed_out = seed(scope, driver)
        check(seed_out["nodes"] >= 2, "그래프 노드 적재", f"nodes={seed_out['nodes']}")
        check(any(n.label == "Work" for n in scope.nodes), "Work 앵커 노드 존재")

        # 3) EXPAND (선택)
        if args.expand:
            print("[3] expand")
            from infra.agent.tracing import make_run_id
            from music_copyright.graphflow.expand import run_expand
            exp = run_expand(driver, work_id=ident.work_id,
                             collected_by=make_run_id("exp"), rounds=1)
            check(exp["rounds_run"] >= 1, "EXPAND 라운드 실행", f"queries={exp['queries_total']}")

        # 4) 보고서
        print("[4] report")
        from music_copyright.graphflow.report import generate_report
        with tempfile.TemporaryDirectory() as out:
            report = generate_report(driver, ident.work_id, out_dir=out)
        check(len(report["sections"]) == 5, "섹션 5개(overview+4축)",
              f"sections={len(report['sections'])}")
        check(len(report["claims"]) > 0, "주장 생성", f"claims={len(report['claims'])}")
        check(len(report["evidence"]) > 0, "증거 인용", f"evidence={len(report['evidence'])}")
        target = report.get("target") or {}
        check(bool(target.get("work_id_type")) and bool(target.get("briefing")),
              "평가 대상 브리핑 생성",
              f"{target.get('title') or '-'} / {target.get('work_id_type') or '-'}")
        val = report["valuation"]
        check(bool(val["overall_grade"]), "종합 등급 판정", val["overall_grade"])
        check(bool(val.get("asset_grade")) and bool(val.get("social_grade")),
              "이중 트랙 등급(자산가치/사회적 가치)",
              f"asset={val.get('asset_grade')} social={val.get('social_grade')}")
        check(len(val["axis_scores"]) == 4, "4축 등급",
              str([a["grade"] for a in val["axis_scores"]]))
    finally:
        close_music_driver()

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"\n=== 결과: {passed}/{total} PASS ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
