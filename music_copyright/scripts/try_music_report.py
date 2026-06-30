"""음악 보고서 end-to-end — resolve → seed → (EXPAND) → generate_report.

real_estate의 try_report에 대응. 공용 run_tracer로 감싸 산출물 + 트레이스가
music_copyright/reports/<run_id>/에 쌓이고 Langfuse run으로 그룹핑된다.
음악 전용 Neo4j(MUSIC_NEO4J_*)를 사용한다.

실행 (음악 Neo4j 가동 + .env 키 필요):
    python -m music_copyright.scripts.try_music_report --title "Dynamite" --artist "BTS"
    python -m music_copyright.scripts.try_music_report --title "Dynamite" --artist "BTS" --expand-rounds 1
    python -m music_copyright.scripts.try_music_report --title "좋니" --artist "윤종신" --workflow
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import music_copyright
from infra.agent.tracing import make_run_id, run_tracer
from infra.env import load_env
from music_copyright.graphflow.graph.driver import close_music_driver, get_music_driver
from music_copyright.graphflow.seeder import seed_from_identity
from music_copyright.resolver import WorkResolver

# 음악은 SQLite가 없다 — db_path는 RunTracer의 run_dir 베이스 앵커로만 쓰인다
# (RunTracer는 db_path.parent/"reports"/run_id만 사용, 파일을 만들지 않음).
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
    p.add_argument("--discourse", action="store_true",
                   help="STORM 담론 모드 (perspectives→discourse→moderator→outline)")
    p.add_argument("--axis", action="append", default=[], metavar="라벨::의도",
                   help="기본 3축 외 평가축 추가 (반복 가능). 예: --axis \"법적 리스크::표절·"
                        "저작권 분쟁, 라이선스 위반, 권리 다툼\"")
    p.add_argument("--discover-axes", action="store_true",
                   help="에이전트(AxisDiscoverer)가 work를 보고 추가 평가축을 자동 제안")
    args = p.parse_args()

    if args.discourse:
        # 담론은 LangGraph 경로 + expand_rounds>0에서만 라우팅된다(workflow.route_after_seed).
        from music_copyright.graphflow.config import set_active_override
        set_active_override({"curation": {"mode": "discourse"}})
        args.workflow = True
        args.expand_rounds = max(1, args.expand_rounds)

    load_env()
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    ua = os.environ.get("MUSICBRAINZ_USER_AGENT")
    lastfm = os.environ.get("LASTFM_API_KEY")
    if not (cid and secret and ua):
        print("에러: SPOTIFY_CLIENT_ID/SECRET, MUSICBRAINZ_USER_AGENT 필요")
        return

    driver = get_music_driver()
    try:
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

            print("[2/3] 그래프 시드 적재 중 (구조 + 정형 보강)...")
            seed_out = seed_from_identity(
                ident, driver, lastfm_api_key=lastfm, mb_user_agent=ua,
                genius_token=os.environ.get("GENIUS_ACCESS_TOKEN"),
                soundcharts_app_id=os.environ.get("SOUNDCHARTS_APP_ID"),
                soundcharts_api_key=os.environ.get("SOUNDCHARTS_API_KEY"),
                embed=True)   # 구조 노드 카드 임베딩 — semantic 검색(SemanticPolicy)에 필요
            print(f"  → {seed_out}")

            # ── 평가 축 해소: 기본 3축 + 사람(--axis) + 에이전트(--discover-axes) ──
            from music_copyright.graphflow.axes import (
                BASE_AXES, AxisDiscoverer, parse_human_axis, resolve_axes)
            from music_copyright.graphflow.graph.queries import work_card_text
            human = [a for a in (parse_human_axis(s) for s in args.axis) if a]
            discovered = []
            if args.discover_axes:
                discovered = AxisDiscoverer().discover(work_card_text(driver, ident.work_id))
            axes = resolve_axes(human=human, discovered=discovered)
            print(f"  기본 평가축: {[a.label for a in BASE_AXES]}")
            if len(axes) > len(BASE_AXES):
                print(f"  추가 평가축: {[a.label for a in axes[len(BASE_AXES):]]}")

            if args.expand_rounds > 0:
                print(f"  EXPAND {args.expand_rounds}라운드 (웹검색 Fact 보강)...")
            print(f"[3/3] 보고서 생성 중 (GPT 집필·종합, 축 {len(axes)}개)... → {out_dir}")
            if args.workflow:
                from music_copyright.graphflow.workflow import run_report_pipeline
                state = run_report_pipeline(ident.work_id, out_dir=out_dir,
                                            evidence_k=args.evidence_k,
                                            expand_rounds=args.expand_rounds, axes=axes)
                report = state["report"]
            else:
                from music_copyright.graphflow.report import generate_report
                report = generate_report(driver, ident.work_id, out_dir=out_dir,
                                         evidence_k=args.evidence_k,
                                         expand_rounds=args.expand_rounds, axes=axes)

        val = report.get("valuation", {})
        print("\n=== 보고서 완료 ===")
        print(f"  report_id : {report['report_id']}")
        print(f"  자산가치 등급 : {val.get('asset_grade') or '-'}")
        print(f"  사회적 가치 등급 : {val.get('social_grade') or '-'}")
        print(f"  종합 등급  : {val.get('overall_grade') or '-'}")
        for a in val.get("axis_scores", []):
            print(f"    - {a['axis']} ({a['grade']}): {a['rationale'][:60]}...")
        print(f"  sections={len(report['sections'])} claims={len(report['claims'])} "
              f"evidence={len(report['evidence'])}")
        print(f"  파일: {out_dir}/report.md , {out_dir}/report.json")
    finally:
        close_music_driver()


if __name__ == "__main__":
    main()
