"""여러 주소를 순차 실행해 trace + 보고서를 누적.

목적: 단일 주소가 아니라 자치구 다양성 6개로 돌려 trace 패턴/데이터 부족 지점 발굴.

산출:
    real_estate/database/DB/reports/{report_id}/  (각 run별)
    real_estate/database/DB/reports/_batch_index.json  (이번 배치 요약)

사용:
    python -m scripts.batch_run                                     # default 6개
    python -m scripts.batch_run --address "서울특별시 강남구 ..."   # 지정한 주소만
    python -m scripts.batch_run --address ADDR1 --address ADDR2 ... # 여러 개
    python -m scripts.batch_run --index-suffix retry                # _batch_index_retry.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from infra.env import load_env
from real_estate.report.agents.base import AgentContext
from real_estate.report.domain import Report, ReportSection
from real_estate.report.llm import get_langfuse_handler
from real_estate.report.rag.sqlite_retriever import SqliteRetriever
from real_estate.report.storage.report_repo import ReportRepository
from real_estate.report.tracing import RunTracer, make_report_id, set_tracer
from real_estate.report.workflow.graph_v2 import (
    build_draft_graph_v2,
    state_to_report_sections_v2,
)
from scripts.run_full_pipeline import DB_PATH, build_asset_context

logger = logging.getLogger("batch_run")


ADDRESSES: list[tuple[str, str | None]] = [
    ("서울특별시 강남구 강남대로 252", "양재타워"),
    ("서울특별시 마포구 와우산로 94", None),
    ("서울특별시 종로구 종로 51", None),
    ("서울특별시 송파구 올림픽로 300", None),
    ("서울특별시 영등포구 여의대로 24", None),
    ("서울특별시 성동구 왕십리로 410", None),
]


def run_one(address: str, building_name: str | None) -> dict:
    """1개 주소에 대해 파이프라인 실행. 실패해도 dict 반환."""
    report_id = make_report_id()
    t0 = time.monotonic()
    tracer = RunTracer(report_id, DB_PATH)
    set_tracer(tracer)
    sigungu = None
    bd_mgt_sn = None
    try:
        tracer.emit("pipeline_input", address=address,
                    building_name=building_name)
        asset_ctx, asset_summary = build_asset_context(address, building_name)
        sigungu = asset_ctx.sigungu
        bd_mgt_sn = asset_ctx.bd_mgt_sn
        tracer.emit("asset_context",
                    asset_id=asset_ctx.asset_id,
                    sigungu=sigungu, sigungu_cd=asset_ctx.sigungu_cd,
                    bd_mgt_sn=bd_mgt_sn,
                    coords=[asset_ctx.latitude, asset_ctx.longitude])

        retriever = SqliteRetriever(str(DB_PATH), asset_ctx)
        ctx = AgentContext(retriever=retriever)
        app = build_draft_graph_v2(ctx, db_path=str(DB_PATH))

        initial = {
            "asset_id": asset_ctx.asset_id,
            "asset_summary": asset_summary,
            "critic_iter": 0,
        }
        invoke_config: dict = {
            "metadata": {
                "report_id": report_id, "address": address,
                "asset_id": asset_ctx.asset_id,
                "sigungu": sigungu, "bd_mgt_sn": bd_mgt_sn,
                "batch_run": True,
            },
        }
        lf = get_langfuse_handler()
        if lf is not None:
            invoke_config["callbacks"] = [lf]
        final_state = app.invoke(initial, config=invoke_config)

        elapsed = time.monotonic() - t0
        sections_data = state_to_report_sections_v2(final_state)
        sections = [ReportSection(
            section_id=f"sec_{uuid.uuid4().hex[:8]}",
            axis=s.get("axis", "overview"),
            title=s.get("title", ""),
            body_markdown=s.get("body_markdown", ""),
            evidence_node_refs=s.get("evidence_node_refs", []),
            evidence_snippets=s.get("evidence_snippets", []),
        ) for s in sections_data]
        report = Report.new(asset_id=asset_ctx.asset_id,
                            asset_summary=asset_summary, report_id=report_id)
        report.append_version(sections, notes=f"batch_run ({elapsed:.1f}s)")
        ReportRepository(DB_PATH).save(report)
        tracer.emit("report_saved", report_id=report.report_id,
                    sections=len(sections), elapsed_sec=round(elapsed, 1))

        critic = final_state.get("critic_output", {})
        return {
            "status": "ok",
            "report_id": report_id,
            "elapsed_sec": round(elapsed, 1),
            "sections": len(sections),
            "sigungu": sigungu, "bd_mgt_sn": bd_mgt_sn,
            "critic_iter": final_state.get("critic_iter", 0),
            "critic_pass": critic.get("overall_pass"),
            "critic_issues": len(critic.get("issues", [])),
        }
    except Exception as exc:
        tracer.emit("error", phase="pipeline",
                    error=type(exc).__name__, message=str(exc)[:300])
        traceback.print_exc()
        return {
            "status": "error", "report_id": report_id,
            "error": type(exc).__name__, "message": str(exc)[:300],
            "sigungu": sigungu, "bd_mgt_sn": bd_mgt_sn,
            "elapsed_sec": round(time.monotonic() - t0, 1),
        }
    finally:
        tracer.close()
        set_tracer(None)


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
    parser.add_argument(
        "--address", action="append", default=None,
        help="주소 (반복 가능). 미지정 시 default ADDRESSES 6개 모두 실행.",
    )
    parser.add_argument(
        "--building", action="append", default=None,
        help="건물명 (--address와 1:1로 페어링; 부족하면 None).",
    )
    parser.add_argument(
        "--index-suffix", default=None,
        help="결과 인덱스 파일을 _batch_index_<suffix>.json으로 저장.",
    )
    args = parser.parse_args()

    if args.address:
        bnames = args.building or []
        addresses = [
            (a, bnames[i] if i < len(bnames) else None)
            for i, a in enumerate(args.address)
        ]
    else:
        addresses = ADDRESSES

    started_at = time.monotonic()
    results: list[dict] = []
    for i, (address, building_name) in enumerate(addresses, 1):
        print(f"\n{'='*70}\n[{i}/{len(addresses)}] {address}\n{'='*70}",
              flush=True)
        try:
            res = run_one(address, building_name)
        except Exception as exc:
            res = {"status": "fatal",
                   "error": type(exc).__name__, "message": str(exc)[:300]}
        res["address"] = address
        res["building_name"] = building_name
        res["idx"] = i
        results.append(res)
        emoji = "OK" if res.get("status") == "ok" else "FAIL"
        print(f"→ {emoji} report_id={res.get('report_id','-')} "
              f"elapsed={res.get('elapsed_sec','?')}s",
              flush=True)

    total_elapsed = time.monotonic() - started_at
    suffix = f"_{args.index_suffix}" if args.index_suffix else ""
    index_path = DB_PATH.parent / "reports" / f"_batch_index{suffix}.json"
    payload = {
        "started_at": started_at,
        "total_elapsed_sec": round(total_elapsed, 1),
        "address_count": len(results),
        "ok_count": sum(1 for r in results if r["status"] == "ok"),
        "results": results,
    }
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    print(f"\n{'='*70}")
    print(f"Batch 완료. {payload['ok_count']}/{len(results)} 성공. "
          f"총 {total_elapsed:.1f}s")
    print(f"인덱스: {index_path}")
    for r in results:
        emoji = "✅" if r["status"] == "ok" else "❌"
        extra = ""
        if r["status"] == "ok":
            extra = (f" sigungu={r.get('sigungu','-')} "
                     f"bd={r.get('bd_mgt_sn') or '없음'} "
                     f"crit_iter={r.get('critic_iter')} "
                     f"pass={r.get('critic_pass')}")
        else:
            extra = f" error={r.get('error','-')}"
        print(f"  {emoji} [{r['idx']}] {r['address']} "
              f"→ {r.get('report_id','-')}{extra}")


if __name__ == "__main__":
    main()
