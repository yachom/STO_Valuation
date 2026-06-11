"""Draft 탭 — 기존 보고서 조회 + 신규 초안 생성."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Iterable

import streamlit as st

from real_estate.report.domain import Report, ReportSection
from real_estate.report.streamlit_app._deps import (
    DB_PATH,
    get_driver,
    get_loaded_env,
    get_repo,
    get_resolver,
    get_retriever,
)
from real_estate.report.streamlit_app._section_render import render_section

logger = logging.getLogger("streamlit.draft")


def _list_recent_reports(limit: int = 30) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT report_id, asset_id, asset_summary, status, created_at "
            "FROM report ORDER BY created_at DESC LIMIT ?", (limit,),
        )
        return [dict(r) for r in cur]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _generate_draft(address: str) -> str:
    """주소 → 새 Report 작성 + 저장. report_id 반환."""
    from real_estate.report.agents.base import AgentContext
    from real_estate.report.workflow.graph_v1 import (
        build_draft_graph,
        state_to_report_sections,
    )

    resolver = get_resolver()
    retriever = get_retriever()

    location = resolver.resolve(address)
    asset_id = f"re_{uuid.uuid4().hex[:10]}"
    asset_summary = (
        f"주소: {address}\n"
        f"도로명: {getattr(location, 'road_address', '')}\n"
        f"지번: {getattr(location, 'jibun_address', '')}\n"
        f"건물관리번호: {getattr(location, 'bd_mgt_sn', '')}\n"
    )
    # bcode 10자리 앞 5자리 = LAWD_CD(sigungu_cd). MOLIT/REB scout 인자.
    # hcode 10자리 앞 8자리 = adstrd_cd (서울 OD 행정동 단위).
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

    app = build_draft_graph(AgentContext(retriever=retriever), db_path=str(DB_PATH))
    final_state = app.invoke({
        "asset_id": asset_id,
        "asset_summary": asset_summary,
        "asset_identifiers": asset_identifiers,
    })

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
    get_repo().save(report)
    return report.report_id


def render_draft_tab() -> None:
    st.header("📝 Draft 보고서")

    env_status = get_loaded_env()
    if env_status["missing"]:
        st.warning(f".env 누락 키: {', '.join(env_status['missing'])} — 신규 작성 불가")

    # ── 신규 초안 작성 ─────────────────────────────────────
    with st.expander("➕ 새 자산 보고서 초안 작성", expanded=False):
        address = st.text_input(
            "자산 주소", value="", placeholder="예: 서울특별시 강남구 테헤란로 152",
        )
        col1, col2 = st.columns([1, 4])
        with col1:
            generate_clicked = st.button(
                "초안 생성", type="primary",
                disabled=bool(env_status["missing"]) or not address.strip(),
            )
        if generate_clicked:
            with st.spinner("LangGraph 파이프라인 실행 중 (planner → 3 analyst → synthesizer → validator)..."):
                try:
                    report_id = _generate_draft(address.strip())
                    st.success(f"새 보고서 생성: {report_id}")
                    st.session_state["selected_report_id"] = report_id
                    st.rerun()
                except Exception as exc:
                    logger.exception("draft 생성 실패")
                    st.error(f"오류: {exc}")

    # ── 기존 보고서 ────────────────────────────────────────
    st.subheader("기존 보고서")
    reports = _list_recent_reports()
    if not reports:
        st.info("저장된 보고서가 없습니다. 위에서 새 초안을 생성하세요.")
        return

    options = {
        f"{r['report_id']} · {r['status']} · {r['asset_summary'][:40]}…":
        r["report_id"] for r in reports
    }
    selected_label = st.selectbox(
        "보고서 선택", list(options.keys()),
        index=_default_index(options, st.session_state.get("selected_report_id")),
    )
    selected_report_id = options[selected_label]
    st.session_state["selected_report_id"] = selected_report_id

    report = get_repo().fetch(selected_report_id)
    if report is None:
        st.error("보고서를 찾을 수 없습니다.")
        return

    st.caption(f"asset_id={report.asset_id} · created_at={report.created_at} · status={report.status}")
    latest = report.latest()
    if latest is None:
        st.warning("이 보고서엔 아직 버전이 없습니다.")
        return

    st.caption(
        f"버전 {len(report.versions)}개 — 표시 중: {latest.version_id} "
        f"({latest.created_at})"
    )

    for section in latest.sections:
        with st.container(border=True):
            render_section(section, key_prefix=latest.version_id)


def _default_index(options: dict, current: str | None) -> int:
    if current is None:
        return 0
    keys = list(options.keys())
    for i, k in enumerate(keys):
        if options[k] == current:
            return i
    return 0
