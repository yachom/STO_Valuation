"""주소 1개 → 멀티 에이전트 초안 보고서 end-to-end.

전제:
    - docker-compose up neo4j
    - ingest_all + update_all_cards + update_all_embeddings 완료
    - .env: ANTHROPIC_API_KEY, NEO4J_*, KAKAO_API_KEY, JUSO_API_KEY

사용:
    python -m scripts.try_report_draft "서울특별시 강남구 테헤란로 152"
"""

import logging
import os
import sys
import uuid
from pathlib import Path

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.report.agents.base import AgentContext
from real_estate.report.domain import Report, ReportSection
from real_estate.report.graph.driver import close_neo4j_driver, get_neo4j_driver
from real_estate.report.tool.retriever.naive_retriever import NaiveNeighborhoodRetriever
from real_estate.report.storage.report_repo import ReportRepository
from real_estate.report.workflow.graph_v1 import (
    build_draft_graph,
    state_to_report_sections,
)

DB_PATH = Path("real_estate/database/DB/real_estate.db")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    missing = [k for k in (
        "ANTHROPIC_API_KEY", "NEO4J_PASSWORD",
        "KAKAO_API_KEY", "JUSO_API_KEY",
    ) if not os.environ.get(k)]
    if missing:
        sys.exit(f"env 누락: {', '.join(missing)}")

    address = " ".join(sys.argv[1:]).strip() or "서울특별시 강남구 테헤란로 152"

    # 1) 주소 정규화 → 자산 요약
    resolver = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )
    location = resolver.resolve(address)
    asset_id = f"re_{uuid.uuid4().hex[:10]}"
    asset_summary = (
        f"주소: {address}\n"
        f"도로명: {getattr(location, 'road_address', '')}\n"
        f"지번: {getattr(location, 'jibun_address', '')}\n"
        f"건물관리번호: {getattr(location, 'bd_mgt_sn', '')}\n"
        f"좌표: ({getattr(location, 'latitude', '')}, {getattr(location, 'longitude', '')})"
    )
    bcode = getattr(location, "bcode", None) or ""
    hcode = getattr(location, "hcode", None) or ""
    asset_identifiers = {
        "bd_mgt_sn": getattr(location, "bd_mgt_sn", None),
        "latitude": getattr(location, "latitude", None),
        "longitude": getattr(location, "longitude", None),
        "sigungu": getattr(location, "sigungu", None),
        "sigungu_cd": bcode[:5] if bcode else None,
        "adstrd_cd": hcode[:8] if hcode else None,
    }

    driver = get_neo4j_driver()
    try:
        retriever = NaiveNeighborhoodRetriever(driver)
        ctx = AgentContext(retriever=retriever)
        app = build_draft_graph(ctx, db_path=str(DB_PATH))

        # 2) graph 실행
        initial = {
            "asset_id": asset_id,
            "asset_summary": asset_summary,
            "asset_identifiers": asset_identifiers,
        }
        final_state = app.invoke(initial)

        # 3) Report 도메인 객체로 변환 + SQLite 저장
        sections_data = state_to_report_sections(final_state)
        sections = [ReportSection(
            section_id=f"sec_{uuid.uuid4().hex[:8]}",
            axis=s.get("axis", "overview"),
            title=s.get("title", ""),
            body_markdown=s.get("body_markdown", ""),
            evidence_node_refs=s.get("evidence_node_refs", []),
            evidence_snippets=s.get("evidence_snippets", []),
        ) for s in sections_data]

        report = Report.new(asset_id=asset_id, asset_summary=asset_summary)
        report.append_version(sections, notes="initial draft")
        ReportRepository(DB_PATH).save(report)

        # 4) 출력
        print(f"\n=== Report {report.report_id} (status={report.status}) ===\n")
        for s in sections:
            print(f"## [{s.axis}] {s.title}\n")
            print(s.body_markdown)
            print(f"\n— refs: {len(s.evidence_node_refs)}개")
            print("\n---\n")
        print(f"\nValidation issues: {final_state.get('validation_issues', [])}")
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    main()
