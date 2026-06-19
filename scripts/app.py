"""STO 상가 부동산 평가 — Streamlit UI (Figma 흐름 반영 + 흐름-비결합 설계).

설계 원칙(중요): **Streamlit은 얇은 표현층.** 에이전트 흐름이 바뀌어도 이 코드를
고치지 않도록 —
  · 진행 상황은 에이전트가 내는 *로그를 그대로 스트리밍*해서 보여준다(흐름-비결합).
    새 에이전트/단계가 생겨도 graphflow/external/infra 로거로 찍히면 자동 표시.
  · 결과는 안정적 출력 계약(report.json: sections/blocks/claims/evidence)으로만 렌더.
    에이전트 내부가 바뀌어도 이 스키마가 유지되면 렌더러 불변.

Figma 8단계 흐름 참고(복제 아님): 주소확인 → 분석(로그) → 보고서+소스패널 →
담당자 피드백(블록 선택)·재검토(검증로그+diff) → 고객 QA(챗) → 다운로드.

실행:  streamlit run scripts/app.py
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # bare top-level import

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


# ── 흐름-비결합 핵심: 에이전트 로그를 그대로 라이브 스트리밍 ──────────
class _QueueHandler(logging.Handler):
    def __init__(self, q): super().__init__(); self._q = q
    def emit(self, record):
        try: self._q.put_nowait(self.format(record))
        except Exception: pass


_STREAM_LOGGERS = ("graphflow", "external", "infra", "external.tavily")


def run_streaming(fn, container, *, tail: int = 60):
    """fn()을 스레드로 돌리며 graphflow/external/infra 로거 INFO를 라이브 표시.

    에이전트 흐름이 바뀌어도 — 새 단계가 이 로거들로 찍히기만 하면 자동으로 보인다.
    Streamlit 코드 수정 불필요. 반환: (fn 결과, 로그 라인들).
    """
    q: queue.Queue = queue.Queue()
    h = _QueueHandler(q)
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(name)s — %(message)s"))
    attached = []
    for name in _STREAM_LOGGERS:
        lg = logging.getLogger(name)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)
        lg.addHandler(h)
        attached.append(lg)

    box: dict = {}
    def work():
        try: box["value"] = fn()
        except Exception as exc: box["error"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    lines: list[str] = []
    try:
        while t.is_alive() or not q.empty():
            got = False
            try:
                while True:
                    lines.append(q.get_nowait()); got = True
            except queue.Empty:
                pass
            container.code("\n".join(lines[-tail:]) or "에이전트 시작 중…", language="log")
            if not got:
                time.sleep(0.25)
        t.join()
    finally:
        for lg in attached:
            lg.removeHandler(h)
    container.code("\n".join(lines[-tail:]) or "(로그 없음)", language="log")
    if "error" in box:
        raise box["error"]
    return box.get("value"), lines


# ── 출처(소스) 분류·번호화 (Figma 04 소스 패널) ──────────────
def _source_badge(ev: dict) -> str:
    sk = ev.get("source_kind"); url = (ev.get("origin") or {}).get("url") or ""
    if sk in ("news_fact", "news_doc"):
        return "🟪 민간"
    if any(d in url for d in ("go.kr", "reb.or.kr", "data.seoul", "eais.go.kr", "localdata")):
        return "🟦 공공"
    return "⬜ 내부"


def _source_index(report: dict):
    ev_map = report.get("evidence", {}); used: list[str] = []
    for cl in report.get("claims", {}).values():
        for r in cl.get("cites", []):
            if r in ev_map and r not in used:
                used.append(r)
    ref_no = {r: i + 1 for i, r in enumerate(used)}
    return ref_no, [(ref_no[r], r, ev_map[r]) for r in used]


def _vicon(s: str) -> str:
    return {"VALID": "🟢", "DECAYED": "🟡", "EXPIRED": "🔴"}.get(s, "⚪")


_STEPS = ["주소 확인", "분석·생성", "1차 보고서", "담당자 피드백", "재검토·수정", "고객 QA"]


def _steps(active: int):
    for c, (i, name) in zip(st.columns(len(_STEPS)), enumerate(_STEPS)):
        mark = "✅" if i < active else ("🔵" if i == active else "⚪")
        color = "" if i == active else "color:#999"
        c.markdown(f"<div style='text-align:center;font-size:.78rem;{color}'>{mark}<br>"
                   f"{'<b>'+name+'</b>' if i==active else name}</div>", unsafe_allow_html=True)
    st.divider()


# ── 보고서 + 소스 패널 + 다운로드 (Figma 04·07) ──────────────
def _render_report(report: dict):
    from real_estate.graphflow.report import render_markdown
    ref_no, src_rows = _source_index(report)
    claims = report.get("claims", {}); val = report.get("valuation") or {}

    main, panel = st.columns([2, 1], gap="large")
    with main:
        st.markdown(f"#### 감정평가 보고서 · {report['version_id']}")
        st.caption(f"`{report['report_id']}` · 생성 {report.get('created_at','')[:16]} "
                   f"· 사용된 소스 {len(src_rows)}개")
        if val.get("overall_grade"):
            st.markdown(f"**사회적 가치 종합 등급: :blue[{val['overall_grade']}]**")
            for mc, a in zip(st.columns(max(1, len(val.get('axis_scores', [])))),
                             val.get("axis_scores", [])):
                mc.metric(a["axis"], a["grade"], help=a["rationale"])
        c1, c2 = st.columns(2)
        c1.download_button("⬇ 보고서 (.md)", render_markdown(report),
                           file_name=f"{report['report_id']}.md")
        c2.download_button("⬇ 보고서 (.json)",
                           json.dumps(report, ensure_ascii=False, indent=2),
                           file_name=f"{report['report_id']}.json")
        st.divider()
        for sec in report["sections"]:
            st.markdown(f"##### [{sec['axis']}] {sec['title']}")
            for b in sec["blocks"]:
                st.markdown(b["markdown"])
                nums = sorted({ref_no[r] for cid in b.get("claim_ids", [])
                               for r in claims.get(cid, {}).get("cites", []) if r in ref_no})
                if nums:
                    st.caption("출처 " + " ".join(f"`[{n}]`" for n in nums))
            st.write("")

    with panel:
        st.markdown(f"##### 📎 사용된 소스 ({len(src_rows)})")
        for no, ref, ev in src_rows:
            o = ev.get("origin") or {}
            name = o.get("press") or o.get("source") or ref.split(":")[0]
            line = f"**[{no}]** {_source_badge(ev)} {_vicon((ev.get('validity') or {}).get('status',''))}  \n{name}"
            if o.get("url"):
                line += f" · [🔗]({o['url']})"
            with st.container(border=True):
                st.markdown(line)
                st.caption((ev.get("text") or "")[:90])


# ── 담당자: 블록 선택 피드백 + 검증 + diff (Figma 05·06) ──────
def _render_revise(driver, report, asset_id, out_dir):
    from real_estate.graphflow.reviser import resolve_feedback, submit_feedback

    st.caption("담당자: **보고서에서 수정할 문단(블록)을 선택** → 피드백 → 에이전트가 그래프 "
               "증거와 대조(Assessment) → 합의 시에만 외과적 수정. 합의 전 보고서 불변.")
    # 블록 선택 = '보고서에서 그 부분 선택' (드래그 대안 — 블록 단위)
    blocks = [(b["block_id"], b["markdown"], sec["axis"])
              for sec in report["sections"] for b in sec["blocks"]]
    labels = {f"[{ax}] {md[:64]}…": bid for bid, md, ax in blocks}

    left, right = st.columns([1, 1], gap="large")
    with left:
        st.markdown("##### ① 수정할 문단 선택")
        sel = st.selectbox("문단", list(labels), key="rv_sel")
        target_id = labels[sel]
        with st.container(border=True):
            st.markdown(next(md for bid, md, _ in blocks if bid == target_id))
        kind = st.radio("피드백 종류", ["edit", "challenge", "enrich"], horizontal=True,
                        help="edit=표현 / challenge=사실이의 / enrich=보강")
        comment = st.text_area("코멘트", key="rv_comment")
        attach = st.text_area("첨부 자료(선택)", key="rv_attach")
        if st.button("피드백 제출 → 평가", type="primary", disabled=not comment):
            box = st.empty()
            atts = [{"text": attach, "provider": "발행자 제공"}] if attach.strip() else None
            res, _ = run_streaming(
                lambda: submit_feedback(driver, report, asset_id, target_id=target_id,
                                        kind=kind, comment=comment, attachments=atts), box)
            st.session_state["rv_assess"] = res
            st.session_state["rv_kind"] = kind
            st.session_state.pop("rv_diff", None)

    with right:
        st.markdown("##### ② 검증 로그 (Assessment)")
        res = st.session_state.get("rv_assess")
        if not res:
            st.info("문단을 고르고 피드백을 제출하면 평가가 표시됩니다.")
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
            decision = ("accept" if c1.button("✔ 수용") else "rebut" if c2.button("↩ 재반박")
                        else "withdraw" if c3.button("✖ 철회") else None)
            if decision:
                before = report
                box = st.empty()
                out, _ = run_streaming(lambda: resolve_feedback(
                    driver, report, asset_id, feedback_id=res["feedback_id"],
                    target_claim_id=res["_target_claim_id"],
                    kind=st.session_state.get("rv_kind", "edit"),
                    comment=st.session_state.get("rv_comment", ""), decision=decision,
                    out_dir=out_dir, rebut_comment=st.session_state.get("rv_comment", "")), box)
                if out.get("report_changed"):
                    st.session_state["rv_diff"] = _diff(before, out["report"])
                    st.session_state["report"] = out["report"]
                    st.session_state.pop("rv_assess", None)
                    st.success(f"수정 적용 → {out['version']} (타깃 외 불변)")
                elif decision == "rebut":
                    st.session_state["rv_assess"] = {**res, **out}
                    st.warning(f"재평가(라운드 {out.get('round')}) — 보고서 불변")
                else:
                    st.session_state.pop("rv_assess", None)
                    st.warning(f"status={out['status']} — 보고서 불변")

    if st.session_state.get("rv_diff"):
        d = st.session_state["rv_diff"]
        st.divider()
        st.markdown(f"##### 변경사항 {d['ver']}")
        st.code(d["text"], language="diff")


def _diff(before: dict, after: dict) -> dict:
    bb = {b["block_id"]: b["markdown"] for s in before["sections"] for b in s["blocks"]}
    ab = {b["block_id"]: b["markdown"] for s in after["sections"] for b in s["blocks"]}
    out = []
    for bid in sorted(set(bb) | set(ab)):
        if bb.get(bid, "") != ab.get(bid, ""):
            out += list(difflib.unified_diff(
                bb.get(bid, "").splitlines(), ab.get(bid, "").splitlines(),
                lineterm="", n=1, fromfile=bid, tofile=bid))[2:]
    return {"ver": f"{before['version_id']} → {after['version_id']}",
            "text": "\n".join(out) or "(블록 텍스트 동일 — 인용/메타만 변경)"}


# ── 고객 QA 챗 (Figma 08) ────────────────────────────────────
_SUGGEST = ["임차 회전율은?", "개발 호재가 있어?", "ESG 등급 근거는?", "배후수요는?"]


def _render_qa(driver, asset_id):
    from real_estate.graphflow.qa import QAResponder
    st.caption("고객: 보고서와 동일 그래프·하이브리드 검색으로 답합니다. 답변엔 출처가 함께.")
    hist = st.session_state.setdefault("qa_hist", [])
    for m in hist:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            for c in m.get("cites", []):
                link = f" [🔗]({c['url']})" if c.get("url") else ""
                st.caption(f"{_vicon(c['validity'])} `[{c['ref'].split(':')[0]}]` {c['source']}{link}")
    picked = next((s for col, s in zip(st.columns(len(_SUGGEST)), _SUGGEST)
                   if col.button(s, key=f"sug_{s}")), None)
    q = picked or st.chat_input("질문 입력")
    if q:
        hist.append({"role": "user", "content": q})
        with st.chat_message("assistant"):
            box = st.empty()
            res, _ = run_streaming(
                lambda: QAResponder().answer(driver, asset_id, q, allow_expand=False), box)
            badge = "🟢 충분" if res.sufficient else "🔴 범위 밖" if res.gap else "🟡 부분"
            hist.append({"role": "assistant", "content": f"{badge}\n\n{res.answer_markdown}",
                         "cites": [{"ref": c.ref, "source": c.source, "url": c.url,
                                    "validity": c.validity_status} for c in res.citations]})
        st.rerun()


# ── 메인 ─────────────────────────────────────────────────────
st.title("🏢 STO 상가 부동산 정성 평가")
ss = st.session_state

with st.sidebar:
    st.header("자산 입력")
    address = st.text_input("평가 대상 주소", "서울 강서구 마곡동 774-1")
    if st.button("① 주소 확인"):
        try:
            from real_estate.location import LocationResolver
            loc = LocationResolver(kakao_api_key=os.environ["KAKAO_API_KEY"],
                                   juso_api_key=os.environ.get("JUSO_API_KEY", "")).resolve(address)
            ss["loc"] = loc
        except Exception as exc:
            st.error(f"주소 해소 실패: {exc}")
    if ss.get("loc"):
        from real_estate.graphflow.seeder import asset_id_for
        loc = ss["loc"]
        aid = asset_id_for(loc)            # loc에서 결정적 유도 (세션 키 의존 X)
        st.success(f"✓ {aid}")
        st.caption(f"{loc.road_address or loc.jibun_address}\n\n({loc.latitude:.5f}, {loc.longitude:.5f})")
        st.map({"lat": [loc.latitude], "lon": [loc.longitude]}, size=40)
        rounds = st.slider("EXPAND 라운드(뉴스 수집)", 0, 3, 1)
        if st.button("② 보고서 생성", type="primary"):
            d = _driver()
            out_dir = str(REPORTS / f"ui_{aid.replace(':','_').replace(',','_')}")
            st.write("**분석 로그** (에이전트 실시간)")
            box = st.empty()
            from real_estate.graphflow.workflow import run_report_pipeline
            try:
                final, logs = run_streaming(lambda: run_report_pipeline(
                    d, asset_id=aid, out_dir=out_dir, db_path=DB_PATH,
                    location=loc, expand_rounds=rounds), box)
                ss["report"] = final["report"]; ss["asset_id"] = aid
                ss["out_dir"] = out_dir; ss["gen_logs"] = logs
                for k in ("rv_assess", "rv_diff", "qa_hist"):
                    ss.pop(k, None)
                st.success("✅ 생성 완료")
            except Exception as exc:
                st.error(f"생성 실패: {exc}")
    if ss.get("report"):
        st.divider()
        st.caption(f"현재: `{ss['report']['report_id']}` {ss['report']['version_id']}")

if not ss.get("report"):
    _steps(0 if not ss.get("loc") else 1)
    st.info("← 주소를 입력하고 **① 주소 확인** → **② 보고서 생성**.")
else:
    d = _driver()
    t1, t2, t3 = st.tabs(["📄 1차 보고서 검토", "✍️ 담당자 피드백·재검토", "💬 고객 QA"])
    with t1:
        _steps(2)
        _render_report(ss["report"])
        if ss.get("gen_logs"):
            with st.expander("🛠 분석 로그 (에이전트 동작)"):
                st.code("\n".join(ss["gen_logs"]), language="log")
    with t2:
        _steps(3)
        _render_revise(d, ss["report"], ss["asset_id"], ss["out_dir"])
    with t3:
        _steps(5)
        _render_qa(d, ss["asset_id"])
