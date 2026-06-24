"""음악 보고서 end-to-end 검증 — resolve → seed → generate_report.

real_estate의 try_report에 대응. 자유텍스트(제목+아티스트) 한 줄로 정성 가치평가
보고서(JSON + markdown)를 생성한다. real_estate와 동일하게 공용 run_tracer로 감싸,
산출물 + 트레이스 사이드카가 자산 내부 run 디렉터리(music_copyright/reports/<run_id>/)에
쌓이고 Langfuse run으로 그룹핑된다.

실행 (NEO4J 가동 + .env 키 필요):
    python -m scripts.try_music_report --title "Dynamite" --artist "BTS"
    python -m scripts.try_music_report --title "좋니" --artist "윤종신" --workflow
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import music_copyright
from infra.agent.tracing import make_run_id, run_tracer
from infra.env import load_env
from infra.graph.driver import close_neo4j_driver, get_neo4j_driver
from music_copyright.graphflow.seeder import seed_from_identity
from music_copyright.resolver import WorkResolver

# 음악은 SQLite가 없다 — db_path는 RunTracer의 run_dir 베이스 앵커로만 쓰인다
# (RunTracer는 db_path.parent/"reports"/run_id만 사용, 파일을 만들지 않음).
# 결과: music_copyright/reports/<run_id>/  (real_estate/database/DB/reports/<run_id>/와 평행)
_MUSIC_HOME = Path(music_copyright.__file__).resolve().parent
_TRACE_ANCHOR = _MUSIC_HOME / "store.db"


def main():
    p = argparse.ArgumentParser(description="음악 보고서 end-to-end")
    p.add_argument("--title", default="Dynamite")
    p.add_argument("--artist", default="BTS")
    p.add_argument("--out", default=None, help="출력 디렉터리 (기본 run_dir)")
    p.add_argument("--evidence-k", type=int, default=None)
    p.add_argument("--expand-rounds", type=int, default=0,
                   help="집필 전 웹검색 EXPAND 라운드 수 (0=생략)")
    p.add_argument("--workflow", action="store_true", help="순차 대신 LangGraph 경로")
    args = p.parse_args()

    load_env()
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    ua = os.environ.get("MUSICBRAINZ_USER_AGENT")
    lastfm = os.environ.get("LASTFM_API_KEY")
    if not (cid and secret and ua):
        print("에러: SPOTIFY_CLIENT_ID/SECRET, MUSICBRAINZ_USER_AGENT 필요")
        return

    driver = get_neo4j_driver()
    try:
        # 1. 식별 (work_id 확보 — 트레이서 세션 키로 사용)
        resolver = WorkResolver(spotify_client_id=cid, spotify_client_secret=secret,
                                musicbrainz_user_agent=ua)
        print(f"질의: {args.artist} - {args.title}\n[1/3] 식별 중...")
        ident = resolver.resolve(title=args.title, artist=args.artist)
        print(f"  → work_id={ident.work_id}  ISWC={ident.iswc}  ISRC={ident.isrc}")

        with run_tracer(make_run_id("rpt"), _TRACE_ANCHOR,
                        session_id=ident.work_id,
                        trace_name=f"music-report:{args.artist}-{args.title}",
                        tags=["report", "music"],
                        metadata={"work_id": ident.work_id, "isrc": ident.isrc,
                                  "iswc": ident.iswc}) as tracer:
            out_dir = args.out or str(tracer.run_dir)

            # 2. 시드 적재
            print("[2/3] 그래프 시드 적재 중...")
            seed_out = seed_from_identity(ident, driver, lastfm_api_key=lastfm,
                                          mb_user_agent=ua)
            print(f"  → {seed_out}")

            # 3. 보고서 생성
            if args.expand_rounds > 0:
                print(f"  EXPAND {args.expand_rounds}라운드 (웹검색 Fact 보강)...")
            print(f"[3/3] 보고서 생성 중 (GPT 집필·종합)... → {out_dir}")
            if args.workflow:
                from music_copyright.graphflow.workflow import run_report_pipeline
                state = run_report_pipeline(ident.work_id, out_dir=out_dir,
                                            evidence_k=args.evidence_k,
                                            expand_rounds=args.expand_rounds)
                report = state["report"]
            else:
                from music_copyright.graphflow.report import generate_report
                report = generate_report(driver, ident.work_id, out_dir=out_dir,
                                         evidence_k=args.evidence_k,
                                         expand_rounds=args.expand_rounds)

        val = report.get("valuation", {})
        print("\n=== 보고서 완료 ===")
        print(f"  report_id : {report['report_id']}")
        print(f"  종합 등급  : {val.get('overall_grade') or '-'}")
        for a in val.get("axis_scores", []):
            print(f"    - {a['axis']} ({a['grade']}): {a['rationale'][:60]}...")
        print(f"  sections={len(report['sections'])} claims={len(report['claims'])} "
              f"evidence={len(report['evidence'])}")
        print(f"  파일: {out_dir}/report.md , {out_dir}/report.json")
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    main()
