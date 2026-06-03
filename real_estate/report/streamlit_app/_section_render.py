"""섹션 렌더 헬퍼 — markdown 본문 + [E#] 인용 chip + evidence expander."""

from __future__ import annotations

import re

import streamlit as st

from real_estate.report.domain import ReportSection

_CITE_PAT = re.compile(r"\[E(\d+)\]")


def render_section(section: ReportSection, *, key_prefix: str = "") -> None:
    st.markdown(f"### [{section.axis}] {section.title}")
    # 본문 markdown 그대로 — citation chip은 사용자 시야 안에 [E#] 형태로 남는다.
    st.markdown(section.body_markdown)

    if section.evidence_snippets:
        with st.expander(f"📎 인용 evidence ({len(section.evidence_node_refs)} 노드)"):
            for i, snippet in enumerate(section.evidence_snippets):
                st.markdown(f"**[E{i+1}]**")
                st.code(snippet, language="text")
            st.caption(
                "node_refs: " + ", ".join(section.evidence_node_refs[:10]) +
                (f" ... 외 {len(section.evidence_node_refs) - 10}개" if len(section.evidence_node_refs) > 10 else "")
            )
    else:
        st.caption("(이 섹션에는 인용된 evidence가 없음)")
