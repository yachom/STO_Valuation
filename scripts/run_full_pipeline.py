"""다중 에이전트 v2 파이프라인 실행 (상가 부동산 평가 모드).

입력: **상가 건물 주소** (Kakao+Juso 해결 → 좌표 + bd_mgt_sn + sigungu).
운영: SQLite retriever + Claude. Neo4j 불필요.

흐름:
    planner (Opus)
      → tenant/esg/commercial v2 analyst (각 Sonnet x2 + Python scout, 인자 검증)
      → synthesizer (Opus)
      → risk_critic (Sonnet, severity gate)
      → 조건부 reviser_v2 (high+medium만)
      → final_editor (Opus)
      → validator
      → END

사용:
    python -m scripts.run_full_pipeline "서울특별시 강남구 강남대로 252"
    python -m scripts.run_full_pipeline --building "양재타워" "서울특별시 ..."
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.report.agents.base import AgentContext
from real_estate.report.domain import Report, ReportSection
from real_estate.report.rag.sqlite_retriever import AssetContext, SqliteRetriever
from real_estate.report.storage.report_repo import ReportRepository
from real_estate.report.tracing import RunTracer, make_report_id, set_tracer
from real_estate.report.workflow.graph_v2 import (
    build_draft_graph_v2,
    state_to_report_sections_v2,
)

DB_PATH = _PROJ / "real_estate" / "database" / "DB" / "real_estate.db"

logger = logging.getLogger("pipeline")


def build_asset_context(address: str, building_name: str | None = None) -> tuple[AssetContext, str]:
    """주소 → LocationResolver → AssetContext + asset_summary 문자열."""
    resolver = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )
    loc = resolver.resolve(address)

    # bcode 10자리 → 앞 5자리 = sigungu_cd (LAWD_CD)
    sigungu_cd = (loc.bcode or "")[:5] if loc.bcode else None
    asset_id = f"store_{uuid.uuid4().hex[:10]}"

    ctx = AssetContext(
        asset_id=asset_id,
        latitude=loc.latitude,
        longitude=loc.longitude,
        bd_mgt_sn=loc.bd_mgt_sn,
        building_name=building_name,
        legal_address=loc.jibun_address,
        road_address=loc.road_address,
        sigungu=loc.sigungu,
        sigungu_cd=sigungu_cd,
        nearby_radius_m=800,
    )
    summary_parts = [f"평가 대상 상가 건물: {address}"]
    if building_name:
        summary_parts.append(f"건물명: {building_name}")
    summary_parts.extend([
        f"도로명: {loc.road_address}",
        f"지번: {loc.jibun_address}",
        f"좌표: ({loc.latitude}, {loc.longitude})",
        f"sido/sigungu/bname: {loc.sido} / {loc.sigungu} / {loc.bname}",
        f"sigungu_cd(LAWD_CD): {sigungu_cd}",
        f"건물관리번호(bd_mgt_sn): {loc.bd_mgt_sn or '미확인'}",
    ])
    return ctx, "\n".join(summary_parts)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()
    missing = [k for k in (
        "ANTHROPIC_API_KEY", "KAKAO_API_KEY", "JUSO_API_KEY",
    ) if not os.environ.get(k)]
    if missing:
        sys.exit(f"env 누락: {', '.join(missing)}")

    parser = argparse.ArgumentParser()
    parser.add_argument("address", nargs="+",
                        help="상가 건물 주소 (자유 형식)")
    parser.add_argument("--building", default=None,
                        help="건물명 (선택)")
    args = parser.parse_args()
    address = " ".join(args.address).strip()

    # report_id를 미리 생성해서 trace 디렉토리 + Report.save에 동일 ID 사용
    report_id = make_report_id()
    logger.info("=== 파이프라인 시작 — address=%s report_id=%s ===", address, report_id)
    t0 = time.monotonic()

    tracer = RunTracer(report_id, DB_PATH)
    set_tracer(tracer)
    try:
        tracer.emit("pipeline_input", address=address, building_name=args.building)

        asset_ctx, asset_summary = build_asset_context(address, args.building)
        logger.info("asset_id=%s, sigungu=%s, sigungu_cd=%s, bd_mgt_sn=%s",
                    asset_ctx.asset_id, asset_ctx.sigungu, asset_ctx.sigungu_cd,
                    asset_ctx.bd_mgt_sn)
        tracer.emit("asset_context",
                    asset_id=asset_ctx.asset_id,
                    sigungu=asset_ctx.sigungu, sigungu_cd=asset_ctx.sigungu_cd,
                    bd_mgt_sn=asset_ctx.bd_mgt_sn,
                    coords=[asset_ctx.latitude, asset_ctx.longitude])

        logger.info("retriever 준비 (배후수요·인접상권·인증 candidate 수집 + 임베딩)...")
        retriever = SqliteRetriever(str(DB_PATH), asset_ctx)

        ctx = AgentContext(retriever=retriever)
        app = build_draft_graph_v2(ctx, db_path=str(DB_PATH))
        logger.info("LangGraph v2 컴파일 완료. invoke 시작...")

        initial = {
            "asset_id": asset_ctx.asset_id,
            "asset_summary": asset_summary,
            "critic_iter": 0,
        }
        final_state = app.invoke(initial)
        elapsed = time.monotonic() - t0
        logger.info("=== 파이프라인 종료 — %.1fs ===", elapsed)
    except Exception as exc:
        tracer.emit("error", phase="pipeline", error=type(exc).__name__,
                    message=str(exc)[:300])
        tracer.close()
        set_tracer(None)
        raise

    # Report 저장
    sections_data = state_to_report_sections_v2(final_state)
    sections = [ReportSection(
        section_id=f"sec_{uuid.uuid4().hex[:8]}",
        axis=s.get("axis", "overview"),
        title=s.get("title", ""),
        body_markdown=s.get("body_markdown", ""),
        evidence_node_refs=s.get("evidence_node_refs", []),
        evidence_snippets=s.get("evidence_snippets", []),
    ) for s in sections_data]
    report = Report.new(asset_id=asset_ctx.asset_id, asset_summary=asset_summary,
                        report_id=report_id)
    report.append_version(sections, notes=f"v2 commercial pipeline ({elapsed:.1f}s)")
    ReportRepository(DB_PATH).save(report)
    logger.info("Report 저장: %s", report.report_id)
    tracer.emit("report_saved", report_id=report.report_id,
                version_id=report.versions[-1].version_id,
                sections=len(sections), elapsed_sec=round(elapsed, 1))

    print("\n" + "=" * 70)
    print(f"Report {report.report_id} (status={report.status})")
    print(asset_summary)
    print("=" * 70)
    for section in report.latest().sections:
        print(f"\n## [{section.axis}] {section.title}\n")
        print(section.body_markdown)
        print(f"\n— refs: {len(section.evidence_node_refs)}, snippets: {len(section.evidence_snippets)}")

    print("\n" + "=" * 70)
    print("=== 에이전트 흐름 요약 ===")
    critic = final_state.get("critic_output", {})
    print(f"  critic_iter: {final_state.get('critic_iter', 0)}")
    print(f"  critic_overall_pass: {critic.get('overall_pass')}")
    print(f"  critic_issues_count: {len(critic.get('issues', []))}")
    for it in critic.get("issues", []):
        print(f"    - [{it.get('severity')}/{it.get('kind')}] "
              f"{it.get('location')}: {(it.get('description') or '')[:80]}")
    for sec in sections_data:
        if sec.get("axis") != "overview":
            c1 = sec.get("confidence_step1")
            c2 = sec.get("confidence_final")
            c1s = f"{c1:.2f}" if isinstance(c1, (int, float)) else "N/A"
            c2s = f"{c2:.2f}" if isinstance(c2, (int, float)) else "N/A"
            print(f"  [{sec['axis']}] conf_step1={c1s} conf_final={c2s} "
                  f"scouts={sec.get('scout_calls_made')}")
    print(f"\n총 소요: {elapsed:.1f}s")
    print(f"산출물:")
    print(f"  {tracer.run_dir}/")
    print(f"  ├── {report.versions[-1].version_id}.md  (보고서 본문)")
    print(f"  ├── trace.jsonl                          (에이전트·LLM·scout 호출 trace)")
    print(f"  └── run.log                              (전체 logger output)")
    tracer.close()
    set_tracer(None)


if __name__ == "__main__":
    main()
