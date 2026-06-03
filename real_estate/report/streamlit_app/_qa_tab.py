"""QA 탭 — final/qa_passed 보고서에 대한 질의응답 chat UI."""

from __future__ import annotations

import logging

import streamlit as st

from real_estate.report.agents.qa import answer_question
from real_estate.report.rag.report_scoped_retriever import ReportScopedRetriever
from real_estate.report.streamlit_app._deps import get_repo

logger = logging.getLogger("streamlit.qa")


def render_qa_tab() -> None:
    st.header("💬 QA — 합의된 보고서 기반 질문")

    repo = get_repo()
    rid = st.session_state.get("selected_report_id")
    if rid is None:
        st.info("Draft 탭에서 보고서를 먼저 선택하세요.")
        return

    report = repo.fetch(rid)
    if report is None:
        st.error("보고서를 찾을 수 없습니다.")
        return

    if report.status not in ("final", "qa_passed"):
        st.warning(
            f"이 보고서는 아직 status={report.status}. "
            "Review 탭에서 'Final로 전이'를 먼저 해야 QA가 가능합니다."
        )
        return

    st.caption(
        f"보고서 {report.report_id} · 상태 {report.status} · "
        f"버전 {len(report.versions)}개"
    )

    # ── retriever를 보고서별로 캐싱 ────────────────────────
    cache_key = f"_qa_retriever__{report.report_id}__{len(report.versions)}"
    if cache_key not in st.session_state:
        with st.spinner("보고서 evidence 임베딩 중..."):
            st.session_state[cache_key] = ReportScopedRetriever(report)
    retriever = st.session_state[cache_key]

    # ── 대화 히스토리 ──────────────────────────────────────
    history_key = f"_qa_history__{report.report_id}"
    if history_key not in st.session_state:
        st.session_state[history_key] = []
    history = st.session_state[history_key]

    for entry in history:
        with st.chat_message(entry["role"]):
            st.markdown(entry["content"])
            if entry.get("citations"):
                with st.expander(f"📎 인용 ({len(entry['citations'])}개)"):
                    for c in entry["citations"]:
                        st.markdown(
                            f"**[E{c['e_index']}]** "
                            f"_{c.get('section_title','')} ({c.get('axis')})_"
                        )
                        st.code(c["snippet_text"], language="text")

    # ── 새 질문 ────────────────────────────────────────────
    question = st.chat_input("이 보고서에 대해 궁금한 점을 물어보세요")
    if not question:
        return

    history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("답변 생성 중..."):
            try:
                result = answer_question(question=question, retriever=retriever)
            except Exception as exc:
                logger.exception("qa failed")
                st.error(f"오류: {exc}")
                return
        st.markdown(result.answer_markdown)
        if result.out_of_scope:
            st.caption("⚠️ 보고서 범위 밖으로 판단되어 일반 답변을 회피했습니다.")
        if result.citations:
            with st.expander(f"📎 인용 ({len(result.citations)}개)"):
                for c in result.citations:
                    st.markdown(
                        f"**[E{c['e_index']}]** "
                        f"_{c.get('section_title','')} ({c.get('axis')})_"
                    )
                    st.code(c["snippet_text"], language="text")

    history.append({
        "role": "assistant",
        "content": result.answer_markdown,
        "citations": result.citations,
    })

    # qa_passed 전이 — 사용자가 응답에 만족하면 보고서를 qa_passed로 닫음
    if report.status == "final" and st.button("✅ 이 답변까지 만족 — QA Passed로 닫기"):
        try:
            report.transition("qa_passed")
            repo.save(report)
            st.success("이 보고서는 QA Passed로 닫혔습니다.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))
