"""다중 에이전트 v2 파이프라인 실행 — Neo4j 없이 SQLite retriever + Claude.

흐름:
    planner (Opus)
      → tenant/esg/commercial v2 analyst (각 Sonnet x2 + Python scout)
      → synthesizer (Opus)
      → risk_critic (Sonnet) → 루프 가능
      → final_editor (Opus)
      → validator (Sonnet)
      → END

사용:
    python -m scripts.run_full_pipeline                  # 단지 id=13 (양재SK허브프리모)
    python -m scripts.run_full_pipeline --complex-id 42
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from infra.env import load_env
from real_estate.report.agents.base import AgentContext
from real_estate.report.domain import Report, ReportSection
from real_estate.report.rag.sqlite_retriever import AssetContext, SqliteRetriever
from real_estate.report.storage.report_repo import ReportRepository
from real_estate.report.workflow.graph_v2 import (
    build_draft_graph_v2,
    state_to_report_sections_v2,
)

DB_PATH = _PROJ / "real_estate" / "database" / "DB" / "real_estate.db"

logger = logging.getLogger("pipeline")


def build_asset_context(complex_id: int) -> tuple[AssetContext, str]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT * FROM apartment_complex WHERE master_complex_id=?",
        (complex_id,),
    ).fetchone()
    conn.close()
    if r is None:
        raise RuntimeError(f"단지 {complex_id} 없음.")

    addr = r["road_address"] or r["legal_address"] or ""
    tokens = addr.split()
    sigungu = tokens[1] if len(tokens) >= 2 else None

    ctx = AssetContext(
        asset_id=f"re_complex_{complex_id}",
        master_complex_id=complex_id,
        latitude=r["latitude"],
        longitude=r["longitude"],
        legal_address=r["legal_address"],
        road_address=r["road_address"],
        sigungu=sigungu,
    )
    summary = (
        f"{r['canonical_name']} ({addr})\n"
        f"좌표: ({r['latitude']}, {r['longitude']})\n"
        f"준공: {r['completion_date']}, 세대수: {r['household_count']}, 동수: {r['total_buildings']}\n"
        f"data_source: {r['data_source']}, 자치구: {sigungu}"
    )
    return ctx, summary


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY 필요 (.env)")

    parser = argparse.ArgumentParser()
    parser.add_argument("--complex-id", type=int, default=13)
    args = parser.parse_args()

    logger.info("=== 파이프라인 시작 — complex_id=%d ===", args.complex_id)
    t0 = time.monotonic()

    # 1) AssetContext + summary
    asset_ctx, asset_summary = build_asset_context(args.complex_id)
    logger.info("asset: %s", asset_summary.splitlines()[0])

    # 2) SqliteRetriever (Neo4j 대체) — bge-m3 임베딩 로드는 한 번
    logger.info("retriever 준비 (bge-m3 임베딩 로드)...")
    retriever = SqliteRetriever(str(DB_PATH), asset_ctx)

    # 3) Build graph + invoke
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

    # 4) Report 저장
    sections_data = state_to_report_sections_v2(final_state)
    sections = [ReportSection(
        section_id=f"sec_{uuid.uuid4().hex[:8]}",
        axis=s.get("axis", "overview"),
        title=s.get("title", ""),
        body_markdown=s.get("body_markdown", ""),
        evidence_node_refs=s.get("evidence_node_refs", []),
        evidence_snippets=s.get("evidence_snippets", []),
    ) for s in sections_data]
    report = Report.new(asset_id=asset_ctx.asset_id, asset_summary=asset_summary)
    report.append_version(sections, notes=f"v2 pipeline ({elapsed:.1f}s)")
    ReportRepository(DB_PATH).save(report)
    logger.info("Report 저장: %s", report.report_id)

    # 5) 출력
    print("\n" + "=" * 70)
    print(f"Report {report.report_id} (status={report.status})")
    print(f"asset: {asset_ctx.asset_id}")
    print(asset_summary)
    print("=" * 70)
    for section in report.latest().sections:
        print(f"\n## [{section.axis}] {section.title}\n")
        print(section.body_markdown)
        print(f"\n— evidence refs: {len(section.evidence_node_refs)}개, "
              f"snippets: {len(section.evidence_snippets)}개")

    # 6) Trace summary (debug)
    print("\n" + "=" * 70)
    print("=== 에이전트 흐름 요약 ===")
    critic = final_state.get("critic_output", {})
    print(f"  critic_iter: {final_state.get('critic_iter', 0)}")
    print(f"  critic_overall_pass: {critic.get('overall_pass')}")
    print(f"  critic_issues_count: {len(critic.get('issues', []))}")
    if critic.get("issues"):
        for it in critic["issues"]:
            print(f"    - [{it.get('severity')}/{it.get('kind')}] "
                  f"{it.get('location')}: {it.get('description')[:80]}")
    validation = final_state.get("validation_issues", [])
    print(f"  validator_issues: {len(validation)}")
    for sec_data in sections_data:
        if sec_data.get("axis") != "overview":
            c1 = sec_data.get("confidence_step1")
            c2 = sec_data.get("confidence_final")
            c1_s = f"{c1:.2f}" if isinstance(c1, (int, float)) else "N/A"
            c2_s = f"{c2:.2f}" if isinstance(c2, (int, float)) else "N/A"
            scouts = sec_data.get("scout_calls_made", [])
            print(f"  [{sec_data['axis']}] conf_step1={c1_s}, conf_final={c2_s}, scouts={scouts}")

    print(f"\n총 소요: {elapsed:.1f}s")
    print(f"sidecar md: real_estate/database/DB/reports/{report.report_id}/")


if __name__ == "__main__":
    main()
