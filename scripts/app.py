"""STO 상가 부동산 평가 — Streamlit UI (Figma 서비스 흐름 반영 고도화).

설계 [[tech_graph_stack]] 'Streamlit + span-level HITL'. Figma 8단계 플로우의 UX를
참고(복제 아님)해 고도화:
  · 진행 스텝 표시 (생성 → 검토 → 피드백 → 재검토 → QA)
  · 📄 보고서 + 사용된 소스 패널 (번호·공공/내부/민간 배지·출처 링크, 블록별 [N] 인용)
  · ✍️ 담당자: 피드백 → Assessment(검증 로그) → 수용 시 변경사항 diff(v→v) + 새 버전
  · 💬 고객 QA: 챗(말풍선 히스토리 + 답변별 출처 칩 + 추천 질문)

실행:  streamlit run scripts/app.py
필요 env: KAKAO/JUSO/ANTHROPIC/NEO4J (+확장 시 NAVER, +Tavily 선택)
"""

from __future__ import annotations

import difflib
import os
import sys
from pathlib import Path

import streamlit as st

# streamlit run은 scripts/만 sys.path에 넣어 bare top-level import가 깨진다 →
# 프로젝트 루트 추가 (python -m 과 동일 환경). [[repo_import_style]]
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


# ── 출처(소스) 분류·번호화 ────────────────────────────────────
def _source_badge(ev: dict) -> str:
    """공공/내부/민간 배지 (Figma 04의 소스 유형)."""
    sk = ev.get("source_kind")
    o = ev.get("origin") or {}
    url = o.get("url") or ""
    if sk in ("news_fact", "news_doc"):
        return "🟪 민간"          # 뉴스/웹
    if any(d in url for d in ("go.kr", "reb.or.kr", "data.seoul", "eais.go.kr", "localdata")):
        return "🟦 공공"          # 공공 API/포털
    return "⬜ 내부"              # SQLite 내부 통계 등


def _source_index(report: dict):
    """보고서가 인용한 evidence ref → 번호. 반환: (ref→no, [(no,ref,ev)] 정렬)."""
    ev_map = report.get("evidence", {})
    used: list[str] = []
    for cl in report.get("claims", {}).values():
        for r in cl.get("cites", []):
            if r in ev_map and r not in used:
                used.append(r)
    ref_no = {r: i + 1 for i, r in enumerate(used)}
    rows = [(ref_no[r], r, ev_map[r]) for r in used]
    return ref_no, rows


def _validity_icon(status: str) -> str:
    return {"VALID": "🟢", "DECAYED": "🟡", "EXPIRED": "🔴"}.get(status, "⚪")


# ── 렌더: 진행 스텝 ──────────────────────────────────────────
_STEPS = ["주소 입력", "분석·생성", "1차 보고서", "담당자 피드백", "재검토·수정", "고객 QA"]


def _step_header(active: int):
    cols = st.columns(len(_STEPS))
    for i, (c, name) in enumerate(zip(cols, _STEPS)):
        mark = "✅" if i < active else ("🔵" if i == active else "⚪")
        c.markdown(f"<div style='text-align:center;font-size:0.8rem'>{mark}<br>"
                   f"<b>{name}</b></div>" if i == active
                   else f"<div style='text-align:center;font-size:0.8rem;color:#888'>"
                   f"{mark}<br>{name}</div>", unsafe_allow_html=True)
    st.divider()


# ── 렌더: 보고서 + 소스 패널 (Figma 04) ──────────────────────
def _render_report(report: dict):
    ref_no, src_rows = _source_index(report)
    claims = report.get("claims", {})
    val = report.get("valuation") or {}

    main, panel = st.columns([2, 1], gap="large")
    with main:
        title = f"감정평가 보고서 · {report['version_id']}"
        st.markdown(f"#### {title}")
        st.caption(f"`{report['report_id']}` · 생성 {report.get('created_at','')[:16]} "
                   f"· 사용된 소스 {len(src_rows)}개")
        if val.get("overall_grade"):
            st.markdown(f"**사회적 가치 종합 등급: :blue[{val['overall_grade']}]**")
            mcols = st.columns(max(1, len(val.get("axis_scores", []))))
            for mc, a in zip(mcols, val.get("axis_scores", [])):
                mc.metric(a["axis"], a["grade"], help=a["rationale"])
        st.divider()
        for sec in report["sections"]:
            st.markdown(f"##### [{sec['axis']}] {sec['title']}")
            for b in sec["blocks"]:
                st.markdown(b["markdown"])
                nums = sorted({ref_no[r] for cid in b.get("claim_ids", [])
                               for r in claims.get(cid, {}).get("cites", [])
                               if r in ref_no})
                if nums:
                    st.caption("출처 " + " ".join(f"`[{n}]`" for n in nums))
            st.write("")

    with panel:
        st.markdown(f"##### 📎 사용된 소스 ({len(src_rows)})")
        for no, ref, ev in src_rows:
            o = ev.get("origin") or {}
            name = o.get("press") or o.get("source") or ref.split(":")[0]
            vic = _validity_icon((ev.get("validity") or {}).get("status", ""))
            line = f"**[{no}]** {_source_badge(ev)} {vic}  \n{name}"
            if o.get("url"):
                line += f" · [🔗]({o['url']})"
            with st.container(border=True):
                st.markdown(line)
                st.caption((ev.get("text") or "")[:90])


# ── 렌더: 담당자 피드백 + 검증 + diff (Figma 05·06) ───────────
def _render_revise(driver, report, asset_id, out_dir):
    from real_estate.graphflow.reviser import resolve_feedback, submit_feedback

    st.caption("담당자: 주장에 피드백 → 에이전트가 그래프 증거와 대조(Assessment) → "
               "**합의 시에만** 외과적 수정. 합의 전 보고서 불변.")
    claims = report["claims"]
    opts = {f"[{c['axis']}/{c['kind']}] {c['text'][:60]}": cid
            for cid, c in claims.items()}
    left, right = st.columns([1, 1], gap="large")

    with left:
        st.markdown("##### 피드백 작성")
        sel = st.selectbox("대상 주장", list(opts), key="rv_sel")
        target_id = opts[sel]
        kind = st.radio("종류", ["edit", "challenge", "enrich"], horizontal=True,
                        help="edit=표현 / challenge=사실이의 / enrich=보강")
        comment = st.text_area("코멘트", key="rv_comment",
                               placeholder="예: 공실률 근거가 약함, 최신 분기 확인 필요")
        attach = st.text_area("첨부 자료(선택)", key="rv_attach",
                              placeholder="예: 관리사무소 자료 — 현재 공실률 6.5%…")
        if st.button("피드백 제출 → 평가", type="primary", disabled=not comment):
            with st.spinner("그래프 증거와 대조 중…"):
                atts = [{"text": attach, "provider": "발행자 제공"}] if attach.strip() else None
                st.session_state["rv_assess"] = submit_feedback(
                    driver, report, asset_id, target_id=target_id, kind=kind,
                    comment=comment, attachments=atts)
                st.session_state["rv_kind"] = kind
                st.session_state.pop("rv_diff", None)

    with right:
        st.markdown("##### 검증 로그 (Assessment)")
        res = st.session_state.get("rv_assess")
        if not res:
            st.info("피드백을 제출하면 에이전트 평가가 여기 표시됩니다.")
        else:
            icon = {"agree": "✅", "disagree": "❌", "partial": "🟡",
                    "inconclusive": "⚪"}.get(res["stance"], "•")
            with st.container(border=True):
                st.markdown(f"{icon} **stance = `{res['stance']}`**")
                st.write(res["rationale"])
                if res.get("proposal"):
                    st.caption(f"제안: {res['proposal']}")
            st.caption("⚖️ 합의 전 보고서 불변")
            c1, c2, c3 = st.columns(3)
            decision = ("accept" if c1.button("✔ 수용") else
                        "rebut" if c2.button("↩ 재반박") else
                        "withdraw" if c3.button("✖ 철회") else None)
            if decision:
                kind = st.session_state.get("rv_kind", "edit")
                before = report
                with st.spinner(f"{decision} 처리…"):
                    out = resolve_feedback(
                        driver, report, asset_id, feedback_id=res["feedback_id"],
                        target_claim_id=res["_target_claim_id"], kind=kind,
                        comment=st.session_state.get("rv_comment", ""),
                        decision=decision, out_dir=out_dir,
                        rebut_comment=st.session_state.get("rv_comment", ""))
                if out.get("report_changed"):
                    st.session_state["rv_diff"] = _build_diff(before, out["report"])
                    st.session_state["report"] = out["report"]
                    st.session_state.pop("rv_assess", None)
                    st.success(f"수정 적용 → 새 버전 {out['version']} (타깃 외 불변)")
                elif decision == "rebut":
                    st.session_state["rv_assess"] = {**res, **out}
                    st.warning(f"재평가(라운드 {out.get('round')}) — 보고서 불변")
                else:
                    st.session_state.pop("rv_assess", None)
                    st.warning(f"status={out['status']} — 보고서 불변")

    diff = st.session_state.get("rv_diff")
    if diff:
        st.divider()
        st.markdown(f"##### 변경사항 {diff['ver']}")
        st.code(diff["text"], language="diff")


def _build_diff(before: dict, after: dict) -> dict:
    """변경된 블록의 markdown 전후 diff (Figma 06 변경사항)."""
    bb = {b["block_id"]: b["markdown"] for s in before["sections"] for b in s["blocks"]}
    ab = {b["block_id"]: b["markdown"] for s in after["sections"] for b in s["blocks"]}
    lines = []
    for bid in sorted(set(bb) | set(ab)):
        if bb.get(bid, "") != ab.get(bid, ""):
            d = difflib.unified_diff(
                bb.get(bid, "").splitlines(), ab.get(bid, "").splitlines(),
                lineterm="", n=1, fromfile=f"{bid}", tofile=f"{bid}")
            lines.extend(list(d)[2:])    # 헤더 2줄 제외
    return {"ver": f"{before['version_id']} → {after['version_id']}",
            "text": "\n".join(lines) or "(블록 텍스트 동일 — 인용/메타만 변경)"}


# ── 렌더: 고객 QA 챗 (Figma 08) ──────────────────────────────
_SUGGEST = ["임차 회전율은 어때?", "개발 호재가 있어?", "ESG 등급 근거는?", "배후수요는?"]


def _render_qa(driver, asset_id):
    from real_estate.graphflow.qa import QAResponder

    st.caption("고객: 보고서와 동일 그래프·하이브리드 검색으로 답합니다. 답변엔 출처가 함께 표시됩니다.")
    hist = st.session_state.setdefault("qa_hist", [])
    for m in hist:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            for c in m.get("cites", []):
                vic = _validity_icon(c["validity"])
                link = f" [🔗]({c['url']})" if c.get("url") else ""
                st.caption(f"{vic} `[{c['ref'].split(':')[0]}]` {c['source']}{link}")

    cols = st.columns(len(_SUGGEST))
    picked = next((s for col, s in zip(cols, _SUGGEST) if col.button(s, key=f"sug_{s}")), None)
    typed = st.chat_input("질문 입력 (예: 비교군 산출 기준은?)")
    q = picked or typed
    if q:
        hist.append({"role": "user", "content": q})
        with st.chat_message("assistant"), st.spinner("그래프 검색·답변 중…"):
            res = QAResponder().answer(driver, asset_id, q, allow_expand=False)
            badge = ("🟢 충분" if res.sufficient else "🔴 범위 밖" if res.gap else "🟡 부분")
            cites = [{"ref": c.ref, "source": c.source, "url": c.url,
                      "validity": c.validity_status} for c in res.citations]
            hist.append({"role": "assistant",
                         "content": f"{badge}\n\n{res.answer_markdown}", "cites": cites})
        st.rerun()


# ── 메인 ─────────────────────────────────────────────────────
st.title("🏢 STO 상가 부동산 정성 평가")
ss = st.session_state

with st.sidebar:
    st.header("① 보고서 생성")
    address = st.text_input("평가 대상 주소", "서울 강서구 마곡동 774-1")
    rounds = st.slider("EXPAND 라운드 (뉴스 수집)", 0, 3, 1,
                       help="0=기존 그래프만(빠름), 1~3=뉴스 수집(수 분)")
    if st.button("🚀 생성", type="primary"):
        try:
            d = _driver()
            loc, aid = _resolve(address)
            out_dir = str(REPORTS / f"ui_{aid.replace(':', '_').replace(',', '_')}")
            with st.status("보고서 생성 중…", expanded=True) as status:
                status.write(f"주소 해소: {aid}")
                from real_estate.graphflow.workflow import run_report_pipeline
                status.write("시드 → 테넌트 → 확장 → 3축 집필 → 종합 …")
                final = run_report_pipeline(
                    d, asset_id=aid, out_dir=out_dir, db_path=DB_PATH,
                    location=loc, expand_rounds=rounds)
                ss["report"] = final["report"]
                ss["asset_id"] = aid
                ss["out_dir"] = out_dir
                for k in ("rv_assess", "rv_diff", "qa_hist"):
                    ss.pop(k, None)
                status.update(label="✅ 생성 완료", state="complete")
        except Exception as exc:
            st.error(f"실패: {exc}")
    if ss.get("report"):
        r = ss["report"]
        st.success(f"`{r['report_id']}` {r['version_id']}")
        st.caption(ss["asset_id"])

if not ss.get("report"):
    _step_header(0)
    st.info("← 사이드바에서 주소를 입력하고 **생성**을 누르세요.")
else:
    d = _driver()
    t1, t2, t3 = st.tabs(["📄 1차 보고서 검토", "✍️ 담당자 피드백·재검토", "💬 고객 QA"])
    with t1:
        _step_header(2)
        _render_report(ss["report"])
    with t2:
        _step_header(3)
        _render_revise(d, ss["report"], ss["asset_id"], ss["out_dir"])
    with t3:
        _step_header(5)
        _render_qa(d, ss["asset_id"])
