"""STO 상가 부동산 평가 — Streamlit UI (보고서 생성 → 담론 → 담당자 수정 → 의뢰자 QA).

설계 [[tech_graph_stack]]의 'Streamlit + span-level HITL'. 네 흐름을 한 앱으로:
  1. 📄 보고서   — 주소 → 전체 파이프라인 → 등급·섹션·근거(출처 링크)
  2. 🧠 담론(STORM) — discourse 모드의 관점 발견·페르소나 라운드테이블 Q&A·아웃라인 표시
  3. ✍️ 발행 담당자 — claim 선택 → 피드백(edit/challenge/enrich) → Assessment(증거 대조)
                     → 수용/철회/재반박 (합의 전 보고서 불변, 수용 시 외과적 수정)
  4. 💬 의뢰자 QA  — 질문 → 하이브리드 검색 답변 + 인용(출처 링크)

큐레이션 모드(사이드바): static(기존·결정적) / moderated(+커버리지 GAP 재진입) /
discourse(STORM 담론 — 관점발견→라운드테이블→스니펫 모더레이터→아웃라인).

실행:  streamlit run scripts/app.py
필요 env: KAKAO/JUSO/ANTHROPIC/NEO4J. moderated/discourse는 +OPENAI(gpt_mini)·NAVER 검색
(+TAVILY 선택 — 일반 웹 모달리티). 라운드 0이면 수집 없이 기존 그래프로만 집필.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

# `streamlit run scripts/app.py`는 scripts/만 sys.path에 넣어 bare top-level
# import(infra/real_estate)가 깨진다 → 프로젝트 루트를 경로에 추가
# (python -m scripts.xxx 와 동일 환경으로). [[repo_import_style]]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.env import load_env

DB_PATH = str(Path(__file__).resolve().parent.parent
              / "real_estate" / "database" / "DB" / "real_estate.db")
REPORTS = Path(DB_PATH).parent / "reports"

st.set_page_config(page_title="STO 상가 평가", page_icon="🏢", layout="wide")


@st.cache_resource
def _driver():
    load_env()
    from real_estate.graphflow.graph.driver import get_neo4j_driver
    return get_neo4j_driver()


def _resolve(address: str):
    from real_estate.location import LocationResolver
    from real_estate.graphflow.seeder import asset_id_for
    loc = LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ.get("JUSO_API_KEY", "")).resolve(address)
    return loc, asset_id_for(loc)


def _trace(prefix: str, **attrs):
    """UI 동작 1건을 run_tracer로 감싼다 — Langfuse trace.name·세션·사이드카 부착.

    try_*.py 스크립트와 동일 패턴. Streamlit/직접호출 경로가 이름 없는 'LangGraph'
    /null trace로 떨어지지 않도록, 보고서·QA·수정 각 동작을 이걸로 감싼다.
    """
    from infra.agent.tracing import make_run_id, run_tracer
    return run_tracer(make_run_id(prefix), DB_PATH, **attrs)


# ── 렌더 헬퍼 ────────────────────────────────────────────────
def _validity_badge(status: str) -> str:
    return {"VALID": "🟢", "DECAYED": "🟡", "EXPIRED": "🔴"}.get(status, "⚪")


def _evidence_line(ref: str, ev: dict) -> str:
    o = ev.get("origin") or {}
    src = o.get("press") or o.get("source") or ev.get("source_kind")
    date = (ev.get("published_at") or "")[:10]
    vb = _validity_badge((ev.get("validity") or {}).get("status", ""))
    link = f" — [🔗 출처]({o['url']})" if o.get("url") else ""
    meta = " · ".join(x for x in (src, date) if x)
    return f"{vb} **`{ref}`** ({meta}){link}\n\n> {ev.get('text','')}"


def _render_report(report: dict):
    val = report.get("valuation") or {}
    if val.get("overall_grade"):
        st.subheader(f"사회적 가치 종합 등급: :blue[{val['overall_grade']}]")
        st.caption(val.get("overall_rationale", ""))
        cols = st.columns(max(1, len(val.get("axis_scores", []))))
        for c, a in zip(cols, val.get("axis_scores", [])):
            c.metric(a["axis"], a["grade"], help=a["rationale"])
    st.divider()

    ev_map = report.get("evidence", {})
    claims = report.get("claims", {})
    for sec in report["sections"]:
        st.markdown(f"### [{sec['axis']}] {sec['title']}")
        sec_refs = set()
        for b in sec["blocks"]:
            st.markdown(b["markdown"])
            for cid in b.get("claim_ids", []):
                for r in claims.get(cid, {}).get("cites", []):
                    if r in ev_map:
                        sec_refs.add(r)
        if sec_refs:
            with st.expander(f"📎 이 섹션의 근거 {len(sec_refs)}건 (출처 링크)"):
                for r in sorted(sec_refs):
                    st.markdown(_evidence_line(r, ev_map[r]))
                    st.markdown("")
        st.divider()


def _render_revise(driver, report, asset_id, out_dir):
    from real_estate.graphflow.feedback import resolve_feedback, submit_feedback

    st.caption("발행 담당자: 주장(claim)에 피드백 → 에이전트가 그래프 증거와 대조해 "
               "Assessment 회신 → **합의 시에만** 외과적 수정 (합의 전 보고서 불변).")
    claims = report["claims"]
    opts = {f"[{c['axis']}/{c['kind']}] {c['text'][:70]}": cid
            for cid, c in claims.items()}
    sel = st.selectbox("대상 주장", list(opts), key="rv_sel")
    target_id = opts[sel]
    kind = st.radio("피드백 종류", ["edit", "challenge", "enrich"], horizontal=True,
                    help="edit=표현수정 / challenge=사실이의 / enrich=보강")
    comment = st.text_area("코멘트", key="rv_comment",
                           placeholder="예: 공실률 근거가 약함, 최신 분기 확인 필요")
    attach = st.text_area("첨부 자료(선택) — 발행자 제공 텍스트", key="rv_attach",
                          placeholder="예: 관리사무소 자료 — 현재 공실률 6.5%…")

    if st.button("피드백 제출 → 평가 받기", type="primary", disabled=not comment):
        with st.spinner("에이전트가 그래프 증거와 대조 중…"):
            atts = [{"text": attach, "provider": "발행자 제공"}] if attach.strip() else None
            with _trace("rev", session_id=asset_id, trace_name=f"revise:{kind}",
                        tags=["revise", "ui"]):
                res = submit_feedback(driver, report, asset_id, target_id=target_id,
                                      kind=kind, comment=comment, attachments=atts)
        st.session_state["rv_assess"] = res

    res = st.session_state.get("rv_assess")
    if res:
        stance_icon = {"agree": "✅", "disagree": "❌", "partial": "🟡",
                       "inconclusive": "⚪"}.get(res["stance"], "")
        st.info(f"**Assessment** {stance_icon} stance=`{res['stance']}`\n\n"
                f"{res['rationale']}" + (f"\n\n**제안:** {res['proposal']}"
                                         if res.get("proposal") else ""))
        st.caption("⚖️ 합의 전 보고서는 변하지 않습니다.")
        c1, c2, c3 = st.columns(3)
        decision = None
        if c1.button("✔ 수용(수정 적용)"): decision = "accept"
        if c2.button("✖ 철회"): decision = "withdraw"
        if c3.button("↩ 재반박"): decision = "rebut"
        if decision:
            with st.spinner(f"{decision} 처리 중…"):
                with _trace("rev", session_id=asset_id,
                            trace_name=f"revise:{kind}:{decision}",
                            tags=["revise", "ui"]):
                    out = resolve_feedback(
                        driver, report, asset_id, feedback_id=res["feedback_id"],
                        target_claim_id=res["_target_claim_id"], kind=kind,
                        comment=comment, decision=decision, out_dir=out_dir,
                        rebut_comment=comment)
            if out.get("report_changed"):
                st.session_state["report"] = out["report"]
                st.success(f"수정 적용 → 새 버전 {out['version']} (타깃 외 불변)")
            elif decision == "rebut":
                st.session_state["rv_assess"] = {**res, "rationale": out["rationale"],
                                                 "stance": out["stance"]}
                st.warning(f"재평가(라운드 {out.get('round')}) — 보고서 불변")
            else:
                st.warning(f"status={out['status']} — 보고서 불변")
            if decision != "rebut":
                st.session_state.pop("rv_assess", None)


def _render_qa(driver, asset_id):
    from real_estate.graphflow.qa import QAResponder

    st.caption("보고서 의뢰자: 자산에 대해 질문 → 보고서와 동일 그래프·하이브리드 검색으로 답변.")
    q = st.text_input("질문", key="qa_q",
                      placeholder="예: 이 건물 임차인 회전율과 개발 호재는 어때?")
    expand = st.checkbox("근거 부족 시 라이브 확장(웹 검색)", value=False)
    if st.button("질문하기", type="primary", disabled=not q):
        with st.spinner("그래프 검색·답변 생성 중…"):
            with _trace("qa", session_id=asset_id, trace_name=f"qa:{q[:30]}",
                        tags=["qa", "ui"]):
                res = QAResponder().answer(driver, asset_id, q, allow_expand=expand)
        st.session_state["qa_res"] = res

    res = st.session_state.get("qa_res")
    if res:
        badge = "🟢 충분" if res.sufficient else ("🔴 범위 밖" if res.gap else "🟡 부분")
        if res.expanded:
            badge += " · 🔄 라이브확장"
        st.markdown(f"**{badge}**")
        st.markdown(res.answer_markdown)
        if res.citations:
            st.markdown("##### 인용 근거")
            for c in res.citations:
                vb = _validity_badge(c.validity_status)
                link = f" — [🔗 출처]({c.url})" if c.url else ""
                new = " 🆕신규수집" if c.is_new else ""
                st.markdown(f"{vb} **`{c.ref}`** ({c.source}{new}){link}\n\n> {c.text}")


def _render_discourse(disc: dict):
    """discourse 모드의 STORM 담론 — 관점(페르소나)·라운드테이블 Q&A·아웃라인."""
    mode = disc.get("mode", "static")
    st.caption(f"이 보고서의 큐레이션 모드: **{mode}**")
    threads = disc.get("threads") or []
    outline = disc.get("outline") or {}
    if mode != "discourse" or not threads:
        st.info("STORM 담론은 **discourse 모드**에서 생성됩니다. 사이드바에서 "
                "'discourse'를 선택하고 라운드 ≥1로 재생성하면 — 관점 발견, 전문가 "
                "페르소나 라운드테이블 Q&A, 담론→아웃라인이 여기 표시됩니다.")
        return

    base = {"tenant", "esg", "commercial"}
    discovered = [t for t in threads if t.get("label") not in base]
    st.markdown(f"**관점 {len(threads)}개** — 3축 척추 ⚓ + 발견 🔸{len(discovered)}개 "
                "(각 전문가 페르소나가 라운드마다 질문, 다른 전문가 발견을 참조)")
    for t in threads:
        is_disc = t.get("label") not in base
        tag = "🔸발견" if is_disc else "⚓척추"
        qa = t.get("qa", [])
        with st.expander(f"{tag} · [{t.get('axis')}] {t.get('persona') or t.get('label')} "
                         f"— {len(qa)}턴"):
            if t.get("focus"):
                st.caption(t["focus"])
            for i, x in enumerate(qa, 1):
                st.markdown(f"**Q{i}.** {x.get('q','')}")
                st.markdown(f"> {x.get('a','')}")
            if not qa:
                st.caption("(수집 없음 — 검색으로 답할 근거를 못 찾음)")

    if outline:
        st.markdown("##### 📋 담론 → 아웃라인 (writer 집필 입력)")
        for ax, pts in outline.items():
            st.markdown(f"**[{ax}]**")
            for p in pts:
                st.markdown(f"- {p}")


# ── 메인 ─────────────────────────────────────────────────────
st.title("🏢 STO 상가 부동산 정성 평가")
ss = st.session_state

with st.sidebar:
    st.header("보고서 생성")
    address = st.text_input("평가 대상 주소", "서울 강서구 마곡동 774-1")
    rounds = st.slider("EXPAND 라운드 (수집)", 0, 3, 1,
                       help="0=기존 그래프만(빠름), 1~3=뉴스·웹 수집(수 분 소요)")
    _MODES = {
        "static": "static — 기존·빠름·결정적",
        "moderated": "moderated — +커버리지 GAP 재진입",
        "discourse": "discourse — STORM 담론(관점·라운드테이블·스니펫 모더레이터)",
    }
    mode = st.selectbox("큐레이션 모드", list(_MODES), format_func=_MODES.get,
                        help="discourse=논문형 다중 전문가 담론. 라운드 ≥1 필요.")
    if mode != "static":
        st.caption("⚠️ moderated/discourse는 뉴스·웹 수집으로 **수 분 소요 + LLM 비용↑** "
                   "(OPENAI·NAVER 키 필요, +TAVILY 선택). 라운드 0이면 적용 안 됨.")

    if st.button("🚀 보고서 생성", type="primary"):
        try:
            d = _driver()
            loc, aid = _resolve(address)
            out_dir = str(REPORTS / f"ui_{aid.replace(':', '_').replace(',', '_')}")
            from real_estate.graphflow.config import set_active_override
            set_active_override({"curation": {"mode": mode}})   # 모드 적용(라우팅)
            steps = ("시드 → 테넌트 → 관점발견 → 라운드테이블 → 모더레이터 → 아웃라인 → 집필 …"
                     if mode == "discourse"
                     else "시드 → 테넌트 → 확장 → 3축 집필 → 종합 …")
            with st.status(f"보고서 생성 중… (모드: {mode})", expanded=True) as status:
                status.write(f"주소 해소: {aid}")
                from real_estate.graphflow.workflow import run_report_pipeline
                status.write(steps)
                with _trace("rpt", session_id=aid, trace_name=f"report:{address}",
                            tags=["report", "ui", mode],
                            metadata={"rounds": rounds, "mode": mode, "asset_id": aid}):
                    final = run_report_pipeline(
                        d, asset_id=aid, out_dir=out_dir, db_path=DB_PATH,
                        location=loc, expand_rounds=rounds)
                ss["report"] = final["report"]
                ss["asset_id"] = aid
                ss["out_dir"] = out_dir
                ss["discourse"] = {"mode": mode, "outline": final.get("outline"),
                                   "threads": final.get("discourse_threads")}
                ss.pop("rv_assess", None)
                ss.pop("qa_res", None)
                status.update(label="✅ 보고서 생성 완료", state="complete")
        except Exception as exc:
            st.error(f"실패: {exc}")
    if ss.get("report"):
        rep = ss["report"]
        st.success(f"현재: `{rep['report_id']}` {rep['version_id']}")
        st.caption(f"asset: {ss['asset_id']}")

if not ss.get("report"):
    st.info("← 사이드바에서 주소를 입력하고 **보고서 생성**을 누르세요.")
else:
    d = _driver()
    t1, t2, t3, t4 = st.tabs(
        ["📄 보고서", "🧠 담론(STORM)", "✍️ 발행 담당자 (수정)", "💬 의뢰자 QA"])
    with t1:
        _render_report(ss["report"])
    with t2:
        _render_discourse(ss.get("discourse") or {})
    with t3:
        _render_revise(d, ss["report"], ss["asset_id"], ss["out_dir"])
    with t4:
        _render_qa(d, ss["asset_id"])
