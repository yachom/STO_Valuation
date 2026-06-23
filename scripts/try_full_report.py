"""풀 E2E 보고서 — 주소 1줄 → discourse 큐레이션 → 최종 보고서. 최대 상세 로그.

    python -m scripts.try_full_report "서울 동작구 상도동 505-1"

제약 없이 전 파이프라인을 discourse mode로 1회 실행:
    SEED → TENANT → [discourse: 관점발견→Q&A다턴→Moderator스윕→Outliner] → 3축 집필 → SYNTH → FINALIZE

discourse mode는 set_active_override로 강제(config.json은 안 건드림). 모든 LLM/툴/이벤트는
사이드카에 풀 기록 — 추후 분석용:
    real_estate/database/DB/reports/{run_id}/
        trace.jsonl              # 타임라인 (확장·에이전트·툴 이벤트 + node/axis 스탬프 + elapsed_ms)
        run.log                  # 로깅 (INFO+)
        llm_calls/{id}.json      # 에이전트별 full system+user 메시지 + 응답 + usage
        tool_calls/{id}.json     # 툴 호출 args + result
        sql_calls/{id}.json      # SQL

필요 env: KAKAO/JUSO/DATA_GO_KR/NAVER/ANTHROPIC/OPENAI/NEO4J (+선택 LANGFUSE/TAVILY)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from infra.env import load_env

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "real_estate" / "database" / "DB" / "real_estate.db"
OUT_DIR = ROOT / "real_estate" / "database" / "DB" / "reports"
_DEFAULT_ADDRESS = "서울 동작구 상도동 505-1"


def main() -> int:
    address = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_ADDRESS

    # graphflow.*는 DEBUG(최대 상세), 라이브러리 잡음은 WARNING으로 억제.
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    logging.getLogger("graphflow").setLevel(logging.DEBUG)
    logging.getLogger("external").setLevel(logging.DEBUG)
    for noisy in ("httpx", "httpcore", "urllib3", "neo4j", "openai", "anthropic",
                  "langchain", "langgraph", "langfuse", "sentence_transformers",
                  "filelock", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("graphflow.full")

    load_env()
    need = ["KAKAO_API_KEY", "JUSO_API_KEY", "DATA_GO_KR_API_KEY",
            "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        print(f"ERROR: .env 필요 키 누락: {missing}", file=sys.stderr)
        return 2

    # discourse mode 강제 (PR-3/4 풀 경로). config.json은 static 유지.
    from real_estate.graphflow.config import get_config, set_active_override
    set_active_override({"curation": {"mode": "discourse"}})
    assert get_config()["curation"]["mode"] == "discourse"
    log.info("=== FULL E2E discourse run ===")
    log.info("주소: %s", address)
    cur = get_config()["curation"]
    log.info("discourse caps: perspectives_max=%s turns=%s research_budget=%s "
             "moderator_rounds=%s", cur["perspectives_max"], cur["turns_per_perspective"],
             cur["research_budget"], cur["moderator_max_rounds"])

    from real_estate.location import LocationResolver
    from real_estate.graphflow.seeder import asset_id_for
    from infra.graph.driver import close_neo4j_driver, get_neo4j_driver
    from real_estate.graphflow.workflow import run_report_pipeline
    from infra.agent.tracing import make_run_id, run_tracer

    location = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"]).resolve(address)
    asset_id = asset_id_for(location)
    log.info("resolved: asset_id=%s", asset_id)
    log.info("location: %s", location)

    driver = get_neo4j_driver()
    run_id = make_run_id("full")
    sidecar = DB_PATH.parent / "reports" / run_id
    log.info("사이드카: %s", sidecar)
    try:
        with run_tracer(run_id, DB_PATH, session_id=asset_id,
                        trace_name=f"full:{address[:30]}", tags=["full", "discourse"],
                        metadata={"asset_id": asset_id, "mode": "discourse",
                                  "address": address}):
            state = run_report_pipeline(
                driver, asset_id=asset_id, out_dir=str(OUT_DIR), db_path=str(DB_PATH),
                location=location, expand_rounds=2)

        report = state.get("report") or {}
        es = state.get("expand_summary") or {}
        outline = state.get("outline") or {}
        print("\n" + "=" * 72)
        print(f"✅ 완료 — REPORT_ID: {report.get('report_id')}")
        print(f"   사이드카(상세로그): {sidecar}")
        if report.get("report_id"):
            print(f"   보고서 파일: {OUT_DIR / report['report_id']}")
        print(f"   EXPAND mode: {es.get('mode')} | 발견관점: {es.get('perspectives_discovered')}")
        print(f"   아웃라인 축: { {ax: len(pts) for ax, pts in outline.items()} }")
        val = (report.get("valuation") or {})
        print(f"   종합 등급: {val.get('overall_grade')}")
        print("=" * 72)
        return 0
    except Exception:
        log.exception("FULL run 실패 — 사이드카에서 마지막 상태 확인: %s", sidecar)
        return 1
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    raise SystemExit(main())
