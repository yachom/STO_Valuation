"""STO_Valuation Streamlit 앱 entrypoint.

사용:
    streamlit run real_estate/report/streamlit_app/main.py

탭:
    📝 Draft   — 신규 초안 생성 + 기존 보고서 조회
    ✏️ Review  — HITL 수정 (PR-9에서 구현)
    💬 QA      — 합의된 보고서 기반 질문 (PR-10에서 구현)
"""

from __future__ import annotations

import logging

import streamlit as st

from real_estate.report.streamlit_app._draft_tab import render_draft_tab


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    st.set_page_config(
        page_title="STO_Valuation — 부동산 사회적 가치 보고서",
        page_icon="🏢",
        layout="wide",
    )
    st.title("🏢 STO_Valuation — 부동산 사회적 가치 보고서")
    st.caption(
        "테넌트 / ESG / 상권 3축 평가. "
        "LangGraph + Neo4j + Microsoft GraphRAG 패턴 + Claude."
    )

    tab_draft, tab_review, tab_qa = st.tabs(["📝 Draft", "✏️ Review", "💬 QA"])
    with tab_draft:
        render_draft_tab()
    with tab_review:
        try:
            from real_estate.report.streamlit_app._review_tab import render_review_tab
            render_review_tab()
        except ImportError:
            st.info("Review 기능은 다음 PR(HITL 수정 루프)에서 활성화됩니다.")
    with tab_qa:
        try:
            from real_estate.report.streamlit_app._qa_tab import render_qa_tab
            render_qa_tab()
        except ImportError:
            st.info("QA 기능은 다음 PR(보고서 기반 질문 응답)에서 활성화됩니다.")


if __name__ == "__main__":
    main()
