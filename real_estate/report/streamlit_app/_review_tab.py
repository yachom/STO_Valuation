"""Review 탭 — span-level 코멘트 + reviser 호출 + 새 버전 생성 + final 전이."""

from __future__ import annotations

import logging
import uuid

import streamlit as st

from real_estate.report.agents.reviser import revise_section
from real_estate.report.domain import Report, ReportSection
from real_estate.report.storage.review_comment_repo import ReviewCommentRepository
from real_estate.report.streamlit_app._deps import DB_PATH, get_repo, get_retriever
from real_estate.report.streamlit_app._section_render import render_section

logger = logging.getLogger("streamlit.review")

_COMMENT_INTENTS = ("factual", "tone", "structure", "missing-evidence")


def render_review_tab() -> None:
    st.header("✏️ Review — span-level HITL")

    repo = get_repo()
    comment_repo = ReviewCommentRepository(str(DB_PATH))

    rid = st.session_state.get("selected_report_id")
    if rid is None:
        st.info("Draft 탭에서 보고서를 먼저 선택하세요.")
        return

    report: Report | None = repo.fetch(rid)
    if report is None:
        st.error("보고서를 찾을 수 없습니다.")
        return

    st.caption(
        f"보고서 {report.report_id} · 상태 {report.status} · "
        f"버전 {len(report.versions)}개"
    )
    if report.status == "qa_passed":
        st.success("이 보고서는 QA 단계까지 완료. 수정 불가.")
        return

    latest = report.latest()
    if latest is None:
        st.warning("버전이 없는 보고서. Draft 탭에서 초안 생성을 해주세요.")
        return

    # ── final 전이 버튼 ────────────────────────────────────
    open_count = sum(
        1 for c in comment_repo.list_for_version(latest.version_id)
        if c.status == "open"
    )
    cols = st.columns([3, 1])
    with cols[0]:
        st.caption(f"열린 코멘트: {open_count}개")
    with cols[1]:
        if report.status != "final" and st.button(
            "✅ Final로 전이", type="primary", disabled=(open_count > 0),
            help="모든 코멘트가 resolved 상태여야 활성화",
        ):
            try:
                report.transition("final")
                repo.save(report)
                st.success("Final로 전이 완료. QA 탭에서 질문 가능.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    # ── 섹션별 패널 ────────────────────────────────────────
    for sec_idx, section in enumerate(latest.sections):
        with st.container(border=True):
            render_section(section, key_prefix=f"{latest.version_id}:{section.section_id}")
            _render_comment_panel(
                section=section,
                version_id=latest.version_id,
                report=report,
                repo=repo,
                comment_repo=comment_repo,
                key_prefix=f"sec_{sec_idx}",
            )


def _render_comment_panel(
    *,
    section: ReportSection,
    version_id: str,
    report: Report,
    repo,
    comment_repo: ReviewCommentRepository,
    key_prefix: str,
) -> None:
    open_comments = comment_repo.list_open_for_section(version_id, section.section_id)
    if open_comments:
        st.markdown(f"**열린 코멘트 ({len(open_comments)})**")
        for c in open_comments:
            anchor_preview = ""
            if c.anchor_start is not None and c.anchor_end is not None:
                anchor_preview = section.body_markdown[c.anchor_start:c.anchor_end][:120]
            st.markdown(
                f"- `[{c.intent}]` {c.instruction}"
                + (f"\n    ↳ 본문 구간: _\"{anchor_preview}\"_" if anchor_preview else "")
            )

    # ── 코멘트 추가 ───────────────────────────────────────
    with st.expander("➕ 코멘트 추가 (span-level)"):
        anchor = st.text_input(
            "본문에서 인용할 일부 (자동으로 위치 매칭)", "",
            key=f"{key_prefix}_anchor",
        )
        intent = st.radio(
            "intent", _COMMENT_INTENTS, horizontal=True,
            key=f"{key_prefix}_intent",
        )
        instruction = st.text_area(
            "수정 지시", "",
            key=f"{key_prefix}_instruction",
        )
        if st.button("💬 코멘트 추가", key=f"{key_prefix}_addcomment"):
            if not instruction.strip():
                st.warning("수정 지시는 비어 있을 수 없습니다.")
            else:
                start, end = None, None
                if anchor:
                    idx = section.body_markdown.find(anchor)
                    if idx >= 0:
                        start, end = idx, idx + len(anchor)
                comment_repo.add(
                    version_id=version_id,
                    section_id=section.section_id,
                    intent=intent,
                    instruction=instruction.strip(),
                    anchor_start=start,
                    anchor_end=end,
                )
                st.success("코멘트가 등록되었습니다.")
                st.rerun()

    # ── reviser 실행 ──────────────────────────────────────
    if open_comments:
        if st.button(
            f"🤖 이 섹션 재작성 (코멘트 {len(open_comments)}개 반영)",
            key=f"{key_prefix}_revise",
        ):
            with st.spinner("Sonnet reviser 호출 중..."):
                try:
                    new_body, new_refs, new_snippets = revise_section(
                        current_body=section.body_markdown,
                        axis=section.axis if section.axis in ("tenant", "esg", "commercial")
                             else "tenant",
                        section_title=section.title,
                        section_question=f"{section.title}에 대한 평가 (재작성)",
                        comments=[
                            {
                                "intent": c.intent,
                                "anchor_start": c.anchor_start,
                                "anchor_end": c.anchor_end,
                                "instruction": c.instruction,
                            }
                            for c in open_comments
                        ],
                        retriever=get_retriever(),
                    )
                    # 새 버전 = 기존 섹션들 + 이 섹션만 교체
                    new_sections = []
                    for s in report.latest().sections:
                        if s.section_id == section.section_id:
                            new_sections.append(ReportSection(
                                section_id=s.section_id,
                                axis=s.axis,
                                title=s.title,
                                body_markdown=new_body,
                                evidence_node_refs=list(dict.fromkeys(
                                    s.evidence_node_refs + new_refs)),
                                evidence_snippets=s.evidence_snippets + new_snippets,
                            ))
                        else:
                            new_sections.append(s)
                    report.append_version(
                        new_sections,
                        notes=f"reviser: section {section.section_id} 코멘트 {len(open_comments)}건 반영",
                    )
                    if report.status == "draft":
                        report.transition("revised")
                    repo.save(report)
                    for c in open_comments:
                        comment_repo.mark_resolved(c.comment_id)
                    st.success("새 버전 저장 완료.")
                    st.rerun()
                except Exception as exc:
                    logger.exception("revise failed")
                    st.error(f"오류: {exc}")
