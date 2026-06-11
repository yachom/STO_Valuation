"""주소 → graph_v3 전체 파이프라인 end-to-end.

graph_v2 (run_full_pipeline.py)와 같은 형식이되 graph_v3 호출.
관전 포인트:
    - planner_v3 → 3 analyst → synthesizer → critic
    - critic fail 시 reflection → planner_v3(replan) → 다시 analyst (최대 2회)
    - 종료 후 discourse_log에 planner_v3 turn이 몇 번 찍혔는지 = multi-call 실증

사용:
    python -m scripts.try_graph_v3 "서울특별시 강남구 테헤란로 152"
    python -m scripts.try_graph_v3 --building "양재타워" "서울특별시 ..."

전제:
    - real_estate.db 적재 완료
    - .env: ANTHROPIC_API_KEY, KAKAO_API_KEY, JUSO_API_KEY
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
from real_estate.report.llm import get_langfuse_handler
from real_estate.report.tool.retriever.sqlite_retriever import (
    AssetContext, SqliteRetriever,
)
from real_estate.report.tool.scout.prefetch import run_prefetch
from real_estate.report.storage.report_repo import ReportRepository
from real_estate.report.tracing import RunTracer, make_report_id, set_tracer
from real_estate.report.workflow.graph_v3 import (
    build_draft_graph_v3,
    state_to_report_sections_v3,
)

DB_PATH = _PROJ / "real_estate" / "database" / "DB" / "real_estate.db"
logger = logging.getLogger("pipeline_v3")


def _build_asset_context(address: str, building_name: str | None):
    resolver = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )
    loc = resolver.resolve(address)
    sigungu_cd = (loc.bcode or "")[:5] if loc.bcode else None
    adstrd_cd = (loc.hcode or "")[:8] if loc.hcode else None
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
    asset_identifiers = {
        "bd_mgt_sn": loc.bd_mgt_sn,
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "sigungu": loc.sigungu,
        "sigungu_cd": sigungu_cd,
        "adstrd_cd": adstrd_cd,
    }
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
    return ctx, asset_identifiers, "\n".join(summary_parts)


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
    parser.add_argument("address", nargs="+", help="상가 건물 주소")
    parser.add_argument("--building", default=None, help="건물명 (선택)")
    args = parser.parse_args()
    address = " ".join(args.address).strip()

    report_id = make_report_id()
    logger.info("=== graph_v3 시작 — address=%s report_id=%s ===",
                address, report_id)
    t0 = time.monotonic()

    tracer = RunTracer(report_id, DB_PATH)
    set_tracer(tracer)
    try:
        tracer.emit("pipeline_input", address=address,
                    building_name=args.building, graph="v3")

        # [1] 사전 정보 확장 — 명시적 단계
        asset_ctx, asset_identifiers, asset_summary = _build_asset_context(
            address, args.building,
        )
        logger.info("asset_id=%s, sigungu=%s, sigungu_cd=%s, bd_mgt_sn=%s",
                    asset_ctx.asset_id, asset_ctx.sigungu, asset_ctx.sigungu_cd,
                    asset_ctx.bd_mgt_sn)
        tracer.emit("asset_context",
                    asset_id=asset_ctx.asset_id,
                    sigungu=asset_ctx.sigungu,
                    sigungu_cd=asset_ctx.sigungu_cd,
                    bd_mgt_sn=asset_ctx.bd_mgt_sn,
                    coords=[asset_ctx.latitude, asset_ctx.longitude])

        logger.info("baseline scout 11종 실행 (prefetch)...")
        legacy_facts, evidence_pool = run_prefetch(
            asset_identifiers, str(DB_PATH),
        )
        logger.info("prefetch 완료 — %d facts", len(evidence_pool))

        # [2] retriever 준비
        logger.info("retriever 준비 (배후수요·상권·인증 candidate 임베딩)...")
        retriever = SqliteRetriever(str(DB_PATH), asset_ctx)

        # [3] graph_v3 컴파일 + invoke
        ctx = AgentContext(retriever=retriever)
        app = build_draft_graph_v3(ctx, db_path=str(DB_PATH))
        logger.info("graph_v3 컴파일 완료. invoke 시작...")

        initial = {
            "asset_id": asset_ctx.asset_id,
            "asset_summary": asset_summary,
            "asset_identifiers": asset_identifiers,
            "evidence_pool": evidence_pool,
            "prefetched_facts": legacy_facts,
            "critic_iter": 0,
            "replan_iter": 0,
        }
        invoke_config: dict = {
            "metadata": {
                "report_id": report_id, "address": address,
                "asset_id": asset_ctx.asset_id,
                "sigungu": asset_ctx.sigungu,
                "bd_mgt_sn": asset_ctx.bd_mgt_sn,
                "graph": "v3",
            },
        }
        lf = get_langfuse_handler()
        if lf is not None:
            invoke_config["callbacks"] = [lf]
        final_state = app.invoke(initial, config=invoke_config)
        elapsed = time.monotonic() - t0
        logger.info("=== graph_v3 종료 — %.1fs ===", elapsed)
    except Exception as exc:
        tracer.emit("error", phase="pipeline", error=type(exc).__name__,
                    message=str(exc)[:300])
        tracer.close()
        set_tracer(None)
        raise

    # Report 저장
    sections_data = state_to_report_sections_v3(final_state)
    sections = [ReportSection(
        section_id=f"sec_{uuid.uuid4().hex[:8]}",
        axis=s.get("axis", "overview"),
        title=s.get("title", ""),
        body_markdown=s.get("body_markdown", ""),
        evidence_node_refs=s.get("evidence_node_refs", []),
        evidence_snippets=s.get("evidence_snippets", []),
    ) for s in sections_data]
    report = Report.new(
        asset_id=asset_ctx.asset_id,
        asset_summary=asset_summary,
        report_id=report_id,
    )
    report.append_version(sections,
                          notes=f"v3 pipeline ({elapsed:.1f}s)")
    ReportRepository(DB_PATH).save(report)
    logger.info("Report 저장: %s", report.report_id)
    tracer.emit("report_saved", report_id=report.report_id,
                version_id=report.versions[-1].version_id,
                sections=len(sections), elapsed_sec=round(elapsed, 1))

    # ── multi-call 실증 출력 ──
    print("\n" + "=" * 70)
    print(f"Report {report.report_id} (status={report.status})")
    print(asset_summary)
    print("=" * 70)
    for section in report.latest().sections:
        print(f"\n## [{section.axis}] {section.title}\n")
        print(section.body_markdown[:600] + ("..." if len(section.body_markdown) > 600 else ""))
        print(f"— refs: {len(section.evidence_node_refs)}, "
              f"snippets: {len(section.evidence_snippets)}")

    print("\n" + "=" * 70)
    print("=== graph_v3 흐름 요약 ===")
    critic = final_state.get("critic_output", {})
    print(f"  replan_iter:        {final_state.get('replan_iter', 0)}")
    print(f"  critic_iter:        {final_state.get('critic_iter', 0)}")
    print(f"  critic_overall_pass: {critic.get('overall_pass')}")
    print(f"  critic_issues:       {len(critic.get('issues', []))}")
    for it in critic.get("issues", []):
        print(f"    - [{it.get('severity')}/{it.get('kind')}] "
              f"{it.get('location')}: {(it.get('description') or '')[:80]}")

    print("\n=== mind_map 최종 ===")
    mm = final_state.get("mind_map", {})
    if mm:
        def _walk(cid, depth):
            c = mm.get(cid)
            if not c:
                return
            indent = "  " * depth
            print(f"{indent}- {c.get('label', cid)} "
                  f"(axis={c.get('axis') or '-'}, origin={c.get('origin','?')}, "
                  f"ev={len(c.get('evidence_ids') or [])})")
            fq = c.get("focus_question")
            if fq:
                print(f"{indent}    Q: {fq[:140]}")
            for cc in c.get("children") or []:
                _walk(cc, depth + 1)
        _walk("root", 0)

    print("\n=== planner_v3 호출 이력 (discourse_log) ===")
    log = final_state.get("discourse_log") or []
    for entry in log:
        print(f"  · turn={entry.get('turn')} mode={entry.get('mode')} "
              f"new_concepts={entry.get('new_concepts') or 'N/A'}")
    print(f"  → planner_v3 가 총 {len(log)}회 호출됨.")

    print(f"\n총 소요: {elapsed:.1f}s")
    print(f"산출물: {tracer.run_dir}/")
    tracer.close()
    set_tracer(None)


if __name__ == "__main__":
    main()
