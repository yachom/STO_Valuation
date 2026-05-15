"""Streamlit UI for STO Social Value Valuation.

실행: streamlit run app.py

기능:
1. 주소 입력 → v2 파이프라인 실행
2. 보고서 / 평가 상세 / 지식그래프 / QA 챗봇 탭
3. 실행 결과를 data/runs/<hash>/에 영속화
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from src.graph import run as run_pipeline
from src.observability import init_langfuse
from src.qa.graph import answer as qa_answer
from src.qa.session import QASession
from src.web.kg_viz import cap_by_degree, filter_subgraph, render_html
from src.web.run_store import list_runs, load_run, save_run


# ---------- 페이지 설정 ----------
st.set_page_config(page_title="STO 사회적 가치 평가", page_icon="📊", layout="wide")

if "lf_inited" not in st.session_state:
    init_langfuse()
    st.session_state["lf_inited"] = True


# ---------- 사이드바 ----------
with st.sidebar:
    st.title("📊 STO Valuation")
    st.caption("LangGraph · Langfuse · Claude")

    st.subheader("🏢 새 평가")
    address_input = st.text_input(
        "주소",
        value=st.session_state.get("last_address", "서울특별시 강남구 테헤란로 152"),
        help="도로명 주소 권장",
    )
    run_button = st.button("📝 보고서 생성", type="primary", use_container_width=True)

    st.divider()
    st.subheader("📁 이전 평가")
    runs = list_runs()
    if runs:
        labels = [f"{r['address']}  ({r['saved_at'][:16] if r['saved_at'] else '?'})" for r in runs]
        idx = st.selectbox("선택", range(len(runs)), format_func=lambda i: labels[i], index=0)
        if st.button("📂 불러오기", use_container_width=True):
            st.session_state["loaded_run_id"] = runs[idx]["id"]
            st.session_state["loaded_address"] = runs[idx]["address"]
            st.session_state["chat_history"] = []
            st.rerun()
    else:
        st.caption("저장된 평가 없음")


# ---------- 실행 ----------
def _run_pipeline_with_progress(address: str):
    with st.status(f"⏳ '{address}' 평가 파이프라인 실행 중…", expanded=True) as status:
        st.write("• 1) 주소 → PNU → 토지/건축물대장 메타데이터 수집")
        st.write("• 2) 뉴스 크롤링 → 온톨로지 태깅 → 지식그래프 적재")
        st.write("• 3) 3축 평가 서브그래프 (Planner → ReAct Retriever → Scorer)")
        st.write("• 4) Claude로 최종 보고서 합성")
        st.write("⌛ 약 2~5분 소요. Langfuse에서 실시간 추적 가능.")
        t0 = time.time()
        try:
            final = run_pipeline(address)
        except Exception as exc:
            status.update(label=f"❌ 실패: {type(exc).__name__}: {exc}", state="error")
            st.exception(exc)
            return None
        elapsed = time.time() - t0
        status.update(label=f"✅ 완료 ({elapsed:.0f}초)", state="complete")
        return final


if run_button and address_input.strip():
    final = _run_pipeline_with_progress(address_input.strip())
    if final:
        report_md = final.get("report_markdown", "")
        kg_json = final.pop("knowledge_graph", None)
        paths = save_run(address_input.strip(), final, report_md, kg_json)
        st.session_state["last_address"] = address_input.strip()
        st.session_state["loaded_run_id"] = Path(paths["dir"]).name
        st.session_state["loaded_address"] = address_input.strip()
        st.session_state["chat_history"] = []
        st.rerun()


# ---------- 로드된 평가 표시 ----------
run_id = st.session_state.get("loaded_run_id")
if not run_id:
    st.title("📊 STO 사회적 가치 평가 시스템")
    st.info("← 사이드바에서 주소를 입력하고 **보고서 생성**을 눌러 시작하세요.\n\n또는 이전 평가를 **불러오기**.")
    st.markdown("""
    ### 파이프라인 개요
    1. 주소 → 메타데이터 (JUSO / VWorld / 건축물대장 / Kakao)
    2. 뉴스 자동 수집 → LLM 온톨로지 태깅 → 지식그래프 구축
    3. 3축 평가 (각 축: Planner → ReAct Retriever → Scorer)
       - **ESG**: 친환경 인증 · 노후도 · 토지이용
       - **인지도**: 네이버·카카오 검색량 신호
       - **상권·입지 활성도**: POI 밀도 · 실거래 · 상권정보
    4. Claude로 종합 보고서 작성

    ### QA 멀티에이전트
    - 보고서 생성 후 **QA 탭**에서 자유 질의 가능
    - 라우터가 적절한 전문가(ReportAnalyst / GraphQuery / AxisDeepDive / LiveTool / CompositeRecalc / Fallback)에게 자동 위임
    """)
    st.stop()


loaded = load_run(run_id)
if not loaded:
    st.error("실행 결과를 찾을 수 없습니다.")
    st.stop()

session: QASession = QASession.load(loaded["report"], loaded["result"], loaded["kg"])
result = session.result
metadata = result.get("metadata") or {}
addr = metadata.get("address") or {}
composite = result.get("composite") or {}

# ---------- 헤더 ----------
col_h1, col_h2, col_h3, col_h4 = st.columns([3, 1, 1, 1])
with col_h1:
    st.title(addr.get("building_name") or addr.get("road") or session.address_label())
    st.caption(addr.get("road") or st.session_state.get("loaded_address", ""))
with col_h2:
    st.metric("종합 점수", composite.get("composite_score", "N/A"))
with col_h3:
    st.metric("등급", composite.get("grade", "N/A"))
with col_h4:
    news = result.get("news") or {}
    g = news.get("graph_summary") or {}
    st.metric("KG 노드/엣지", f"{g.get('nodes', 0)}/{g.get('edges', 0)}")


tab_report, tab_axes, tab_kg, tab_qa = st.tabs(["📄 보고서", "🔬 평가 상세", "🕸 지식그래프", "💬 QA 챗봇"])


# ---------- Tab 1: 보고서 ----------
with tab_report:
    st.markdown(session.report_text)
    st.divider()
    with open(loaded["report"], "rb") as f:
        st.download_button("⬇️ report.md 다운로드", f, file_name="report.md", mime="text/markdown")
    with open(loaded["result"], "rb") as f:
        st.download_button("⬇️ result.json 다운로드", f, file_name="result.json", mime="application/json")


# ---------- Tab 2: 평가 상세 ----------
with tab_axes:
    axis_keys = [("esg", "ESG"), ("awareness", "인지도"), ("vitality", "상권·입지")]
    rows = []
    for k, label in axis_keys:
        a = result.get(k) or {}
        rows.append({
            "축": label,
            "점수": a.get("score"),
            "등급": a.get("grade"),
            "도구호출 수": len(a.get("tool_calls") or []),
            "강점": "; ".join(a.get("strengths") or [])[:80],
            "약점": "; ".join(a.get("weaknesses") or [])[:80],
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()
    for k, label in axis_keys:
        a = result.get(k) or {}
        with st.expander(f"### {label} — {a.get('score')}점 / {a.get('grade')}"):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Planner 가설**")
                st.write(a.get("plan", {}).get("hypothesis", "(없음)"))
                st.markdown("**근거 요약 (rationale)**")
                st.write(a.get("rationale", ""))
            with c2:
                st.markdown("**강점 / 약점 / 리스크**")
                for sect in ("strengths", "weaknesses", "risks"):
                    items = a.get(sect) or []
                    if items:
                        st.markdown(f"_{sect}_")
                        for it in items:
                            st.write(f"- {it}")

            tool_calls = a.get("tool_calls") or []
            if tool_calls:
                st.markdown(f"**도구 호출 ({len(tool_calls)}회)**")
                for i, tc in enumerate(tool_calls):
                    with st.expander(f"{i+1}. {tc.get('tool')}", expanded=False):
                        st.code(tc.get("content", "")[:4000], language="json")


# ---------- Tab 3: 지식그래프 ----------
with tab_kg:
    news = result.get("news") or {}
    fetch = news.get("fetch") or {}
    g_summary = news.get("graph_summary") or {}

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("뉴스 수집", fetch.get("total_unique", 0))
    with col2:
        st.metric("태깅 성공", news.get("tagged_count", 0))
    with col3:
        st.metric("KG 엣지", g_summary.get("edges", 0))

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**노드 타입 분포**")
        nt = g_summary.get("node_types") or {}
        if nt:
            st.dataframe(pd.DataFrame(list(nt.items()), columns=["type", "count"]).sort_values("count", ascending=False), use_container_width=True, hide_index=True)
    with c2:
        st.markdown("**관계 타입 분포**")
        rt = g_summary.get("relation_types") or {}
        if rt:
            st.dataframe(pd.DataFrame(list(rt.items()), columns=["relation", "count"]).sort_values("count", ascending=False), use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("### 🕸 인터랙티브 그래프")
    if session.kg and session.kg.g.number_of_nodes() > 0:
        ctrl_a, ctrl_b, ctrl_c = st.columns([1, 1, 2])
        with ctrl_a:
            axis_filter = st.selectbox(
                "축 필터",
                ["ALL", "ESG", "AWARENESS", "VITALITY"],
                key="kg_axis_filter",
            )
        with ctrl_b:
            max_nodes = st.slider("최대 노드 수", 30, 400, 150, step=10, key="kg_max_nodes")
        with ctrl_c:
            st.caption(
                "Asset(빨강) / Axis(보라) / NewsArticle(파랑) / 엔티티(타입별 색상). "
                "엣지 색: 초록=positive · 빨강=negative · 회색=중립. 노드/엣지에 hover하면 상세."
            )

        sub = filter_subgraph(session.kg.g, axis_filter)
        sub = cap_by_degree(sub, max_nodes)
        st.caption(f"표시: 노드 {sub.number_of_nodes()} · 엣지 {sub.number_of_edges()}")
        if sub.number_of_nodes() == 0:
            st.info("선택한 필터에 해당하는 노드가 없습니다.")
        else:
            html = render_html(sub)
            st.components.v1.html(html, height=680, scrolling=True)
    else:
        st.info("지식그래프가 비어 있습니다.")

    st.divider()
    st.markdown("### 축별 시그널 (AFFECTS_AXIS)")
    if session.kg:
        for axis_key, label in [("ESG", "ESG"), ("AWARENESS", "인지도"), ("VITALITY", "상권·입지")]:
            ev = session.kg.axis_evidence(axis_key, top_k=20)
            with st.expander(f"{label}  —  {len(ev)} 시그널"):
                if not ev:
                    st.caption("시그널 없음")
                    continue
                df_rows = []
                for e in ev:
                    art = e.get("article") or {}
                    df_rows.append({
                        "polarity": e.get("polarity"),
                        "strength": e.get("strength"),
                        "note": (e.get("note") or "")[:200],
                        "기사": art.get("title"),
                        "발행": art.get("pubDate"),
                        "링크": art.get("link"),
                    })
                st.dataframe(pd.DataFrame(df_rows), use_container_width=True, hide_index=True)


# ---------- Tab 4: QA ----------
with tab_qa:
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []
    if "qa_session" not in st.session_state or st.session_state.get("qa_session_run_id") != run_id:
        st.session_state["qa_session"] = session
        st.session_state["qa_session_run_id"] = run_id
        st.session_state["chat_history"] = []

    st.caption("이 보고서·지식그래프 위에서 자유 질의. 라우터가 적절한 전문가에게 위임합니다.")

    suggestions = [
        "ESG 점수가 왜 이렇게 나왔어?",
        "강남파이낸스센터 매각 관련 뉴스 알려줘",
        "반경 500m 카페 개수 다시 세줘",
        "ESG 50%, 인지도 20%, 상권 30%로 종합 점수 재계산",
    ]
    cols = st.columns(len(suggestions))
    for i, s in enumerate(suggestions):
        with cols[i]:
            if st.button(s, key=f"sug_{i}", use_container_width=True):
                st.session_state["pending_question"] = s

    for turn in st.session_state["chat_history"]:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("routing"):
                with st.expander("🔀 라우팅"):
                    st.json(turn["routing"])
            if turn.get("specialist_answers"):
                with st.expander("🧩 전문가별 원본 답변"):
                    for name, ans in turn["specialist_answers"].items():
                        st.markdown(f"**[{name}]**")
                        st.markdown(ans)

    question = st.chat_input("질문을 입력하세요…") or st.session_state.pop("pending_question", None)
    if question:
        st.session_state["chat_history"].append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("멀티에이전트 추론 중…"):
                try:
                    result_qa = qa_answer(question, st.session_state["qa_session"])
                except Exception as exc:
                    st.error(f"실패: {type(exc).__name__}: {exc}")
                    st.stop()
            st.markdown(result_qa["answer"])
            with st.expander("🔀 라우팅"):
                st.json(result_qa["routing"])
            with st.expander("🧩 전문가별 원본 답변"):
                for name, ans in result_qa["specialist_answers"].items():
                    st.markdown(f"**[{name}]**")
                    st.markdown(ans)
        st.session_state["chat_history"].append({
            "role": "assistant",
            "content": result_qa["answer"],
            "routing": result_qa["routing"],
            "specialist_answers": result_qa["specialist_answers"],
        })
