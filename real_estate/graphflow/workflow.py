"""LangGraph 워크플로우 — 보고서 파이프라인의 표준 진입점 (설계 §7).

    SEED → EXPAND → [write_tenant ∥ write_esg ∥ write_commercial] → SYNTH → FINALIZE

- 3축 Writer는 LangGraph fan-out으로 병렬 실행 (LLM I/O 동시) — 순차 대비 ~3배.
- EXPAND는 노드 1개가 run_expand(내부 라운드 루프 + novelty 종료)를 호출 —
  라운드를 LangGraph 사이클로 펴는 것은 중복이라 phase 단위로 둔다.
- AUDIT 기계층은 write/synth 노드 안(재작성 1회), LLM층은 finalize 안 —
  PR-F에서 critic 재진입·발행자 합의 루프(interrupt/checkpoint)가 이 그래프에
  조건 엣지로 붙는다.
- Langfuse: get_langfuse_handler를 invoke config callbacks로 전달 — 그래프
  단위 trace가 LLM/agent span들의 상위로 잡힌다.

사용:
    app = build_report_graph()
    state = run_report_pipeline(driver, asset_id=..., out_dir=..., expand_rounds=0)
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from neo4j import Driver

from real_estate.graphflow.profiles import REPORT_AXES
from real_estate.graphflow.state import GraphFlowState
from infra.agent.tracing import emit, langchain_trace_metadata, make_run_id

logger = logging.getLogger("graphflow.workflow")

_driver: Driver | None = None        # 노드 공유 (Driver는 스레드 안전)


def _get_driver() -> Driver:
    global _driver
    if _driver is None:
        from infra.graph.driver import get_neo4j_driver
        _driver = get_neo4j_driver()
    return _driver


# ───────────────────────────────────────────────────────────
# 노드
# ───────────────────────────────────────────────────────────
def seed_node(state: GraphFlowState) -> dict:
    """location이 있으면 스코프 시드 (idempotent). asset_card 확보."""
    from real_estate.graphflow.report import asset_card_text
    from real_estate.graphflow.seeder import collect_scope, seed

    driver = _get_driver()
    if state.get("location") is not None:
        seed(collect_scope(state["location"], state["db_path"]), driver)
    return {"asset_card": asset_card_text(driver, state["asset_id"])}


def _documents_config() -> dict:
    from real_estate.graphflow.config import get_config
    return get_config().get("documents", {})


def doc_intake_node(state: GraphFlowState) -> dict:
    """선택적 PDF 입력 축 — 표제부 파싱 → doc_requester 판정 → (필요 시) HITL interrupt.

    pdf_path가 없거나 documents.enabled=false면 **즉시 no-op({})** — 주소-only 경로는
    100% 불변(이 노드가 그래프에 있어도 영향 0).

    PDF가 주어지면:
      1) 표제부를 파싱·추출하고 doc_requester로 '추가 전유부가 필요한 호'를 판정.
      2) 추가 문서가 필요하고 interrupt가 켜졌고 아직 그 문서들이 업로드되지 않았으면
         `interrupt(payload)`로 그래프를 멈춰 사람에게 요청(gov.kr URL + 호수 목록).
         재개(Command(resume=업로드문서))되면 interrupt()가 그 값을 돌려주고 계속 진행.
      3) 필요 없거나 이미 업로드됐으면 그대로 통과.

    interrupt()는 checkpointer로 컴파일된 그래프에서만 동작한다(build_report_graph가
    pdf_path 있을 때 MemorySaver로 컴파일). 산출은 document_intake(판정 결과) +
    pending_doc_requests(미충족 요청) + uploaded_documents(재개 시 주입분).
    """
    from langgraph.types import interrupt

    pdf_path = state.get("pdf_path")
    cfg = _documents_config()
    if not pdf_path or not cfg.get("enabled", True):
        return {}

    from real_estate.external.gov24.extract import extract_from_clean_pdf
    from real_estate.external.gov24.parser import parse_gov24_pdf
    from real_estate.graphflow.agents.doc_requester import (
        DocRequester, format_human_doc_request)

    parsed = parse_gov24_pdf(pdf_path)
    extracted = extract_from_clean_pdf(parsed)
    emit("doc_intake_parsed", pdf=parsed.filename, building_id=extracted.building_id,
         changes=len(extracted.changes), rows=len(extracted.building_rows))

    result = DocRequester().decide(
        parsed, extracted,
        min_priority=cfg.get("min_priority", "medium"),
        use_llm=cfg.get("use_llm", True))
    emit("doc_requester_decided", decided_by=result.decided_by,
         needs_more=result.needs_more_documents,
         requests=[r.unit_id for r in result.requests])

    out: dict = {"document_intake": result.to_jsonable()}

    already = state.get("uploaded_documents") or []
    requested_units = {r.unit_id for r in result.requests}
    have_units = {d.get("unit_id") for d in already if isinstance(d, dict)}
    unmet = [r for r in result.requests if r.unit_id not in have_units]

    if not result.needs_more_documents or not unmet:
        out["pending_doc_requests"] = []
        if requested_units and not unmet:
            emit("doc_intake_satisfied", units=sorted(requested_units))
        return out

    if not cfg.get("interrupt", True):
        # interrupt 비활성 — 요청만 surface하고 진행(비차단 모드).
        out["pending_doc_requests"] = [
            {"request_id": r.request_id, "unit_id": r.unit_id,
             "priority": r.priority, "reason": r.reason} for r in unmet]
        return out

    # HITL — 그래프를 멈추고 사람에게 추가 전유부를 요청.
    payload = format_human_doc_request(result)
    emit("doc_intake_interrupt", units=payload["requested_units"])
    supplied = interrupt(payload)   # 재개 시 사람이 넘긴 업로드 문서 리스트가 반환됨

    merged = list(already) + list(supplied or [])
    return {"document_intake": result.to_jsonable(),
            "uploaded_documents": merged,
            "pending_doc_requests": []}


def tenant_node(state: GraphFlowState) -> dict:
    """입점업체 수집(staleness 90일 게이트) + 건물유형 분류.

    수집은 분리 원칙(보고서는 그래프만 읽음) — 동일 자산 재실행 시 collected_at이
    신선하면 스크래핑을 건너뛰고 분류만 한다. location/KAKAO 키가 없으면(기존
    그래프 재사용 모드) 그래프의 기존 Tenant로 분류만 시도한다.
    """
    import os

    from real_estate.repository.building_type import classify_building_type
    driver = _get_driver()
    aid = state["asset_id"]
    with driver.session() as s:
        b = s.run("MATCH (:Asset {asset_id:$a})-[:ANCHORS]->(b:Building) "
                  "RETURN b.name AS n, b.main_use AS mu LIMIT 1", a=aid).single()
    building_name = (b or {}).get("n")
    main_use = (b or {}).get("mu")

    summary: dict = {"skipped": True, "reason": "no_location_or_key"}
    loc = state.get("location")
    kakao = os.environ.get("KAKAO_API_KEY")
    if loc is not None and kakao:
        from real_estate.graphflow.tenant_collector import collect_tenants
        summary = collect_tenants(driver, aid, location=loc, kakao_key=kakao,
                                  reb_key=os.environ.get("REB_LEASE_MARKET_KEY"),
                                  localdata_key=os.environ.get("DATA_GO_KR_API_KEY"),
                                  building_name=building_name)

    # 그래프의 Tenant(방금/기존 적재)로 건물유형 분류.
    with driver.session() as s:
        rows = s.run("MATCH (:Asset {asset_id:$a})-[:HAS_TENANT]->(t:Tenant) "
                     "RETURN t.카테고리 AS 카테고리", a=aid).data()
    btype = classify_building_type(main_use, rows)
    return {"tenant_summary": summary, "building_type": btype}


def _office_purpose(building_type: dict | None) -> str | None:
    """오피스/혼합형이면 navigator를 입주사 식별 쪽으로 유도하는 purpose."""
    if not building_type or building_type.get("앵커전략") not in ("뉴스기반", "혼합"):
        return None
    return (f"이 건물은 '{building_type.get('유형')}'으로 사무실/업무 비중이 있어 "
            "카카오 지도에 입주 기업이 거의 잡히지 않는다. 입주 기업·임차사·"
            "본사/지점 이전·사옥 등 '핵심 입주사 식별'에 도움되는 검색어를 우선 "
            "생성하라(예: '{건물명} 입주 기업', '{건물명} 임차').")


def _curation_mode() -> str:
    from real_estate.graphflow.config import get_config
    return get_config().get("curation", {}).get("mode", "static")


def expand_node(state: GraphFlowState) -> dict:
    """고정 EXPAND (static/moderated 경로). rounds=0이면 생략.

    건물유형이 오피스/혼합이면 navigator에 입주사 식별 purpose를 주입한다(②).
    'moderated'(PR-2)면 EXPAND 뒤 커버리지 GAP→Moderator→미니EXPAND 재진입을 얹는다.
    'discourse'(PR-3/6)는 이 노드를 타지 않고 별도 STORM 담론 서브그래프로 라우팅된다
    (route_after_tenant). 'static'은 기존 결정 경로 100% 그대로.
    """
    rounds = state.get("expand_rounds", 0)
    if rounds <= 0:
        return {"expand_summary": {"rounds_run": 0, "skipped": True}}
    collected_by = state.get("collected_by", make_run_id("exp"))
    from real_estate.graphflow.expand import run_expand
    summary = run_expand(
        _get_driver(), asset_id=state["asset_id"],
        collected_by=collected_by,
        rounds=rounds,
        total_query_budget=state.get("expand_budget"),   # None → config.expand
        purpose=_office_purpose(state.get("building_type")))

    if _curation_mode() == "moderated":
        from real_estate.graphflow.moderator import run_moderated_expand
        summary["moderator"] = run_moderated_expand(
            _get_driver(), asset_id=state["asset_id"], collected_by=collected_by)
    return {"expand_summary": summary}


# ───────────────────────────────────────────────────────────
# STORM 담론 노드 (discourse mode, PR-6) — 토폴로지가 곧 논문 다이어그램
#   perspectives → discourse_round ⟲ moderator → outline
# 각 노드는 discourse.py의 스테이지를 호출(단일 진실). 사이클은 조건부 엣지.
# ───────────────────────────────────────────────────────────
def perspectives_node(state: GraphFlowState) -> dict:
    """STORM 1단계 — 관점 발견 → 초기 담론 스레드."""
    from real_estate.graphflow.discourse import perspectives_stage
    threads = perspectives_stage(
        _get_driver(), state["asset_id"], state.get("building_type"))
    return {"discourse_threads": threads, "discourse_round": 0, "moderator_done": False}


def discourse_round_node(state: GraphFlowState) -> dict:
    """STORM Q&A — 한 라운드: 각 관점이 한 턴씩(공유 담론). 사이클로 심화."""
    from real_estate.graphflow.discourse import discourse_round_stage
    collected_by = state.get("collected_by", make_run_id("exp"))
    threads = discourse_round_stage(
        _get_driver(), state["asset_id"], state["discourse_threads"], collected_by)
    return {"discourse_threads": threads,
            "discourse_round": state.get("discourse_round", 0) + 1}


def moderator_node(state: GraphFlowState) -> dict:
    """Co-STORM 모더레이터 — 커버리지 공백 메우고 계속/종료 판정(사이클 게이트)."""
    from real_estate.graphflow.discourse import discourse_max_rounds, moderator_round_stage
    collected_by = state.get("collected_by", make_run_id("exp"))
    res = moderator_round_stage(
        _get_driver(), state["asset_id"], state.get("discourse_round", 0),
        discourse_max_rounds(), collected_by)
    return {"moderator_done": res["done"]}


def outline_node(state: GraphFlowState) -> dict:
    """STORM 아웃라인 — 담론 → 축별 논점. write 노드가 이 순서로 집필."""
    from real_estate.graphflow.discourse import outline_stage
    outline = outline_stage(_get_driver(), state["asset_id"], state["discourse_threads"])
    return {"outline": outline,
            "expand_summary": {"mode": "discourse",
                               "rounds_run": state.get("discourse_round", 0)}}


def route_after_tenant(state: GraphFlowState) -> str:
    """discourse mode + 확장 켜짐 → STORM 담론 서브그래프, 아니면 고정 expand 노드."""
    if _curation_mode() == "discourse" and state.get("expand_rounds", 0) > 0:
        return "perspectives"
    return "expand"


def route_after_moderator(state: GraphFlowState) -> str:
    """담론 사이클 종료 판정 — moderator done 또는 max_rounds 도달 → outline, 아니면 계속."""
    from real_estate.graphflow.discourse import discourse_max_rounds
    if state.get("moderator_done") or state.get("discourse_round", 0) >= discourse_max_rounds():
        return "outline"
    return "discourse_round"


def _make_write_node(axis: str):
    def node(state: GraphFlowState) -> dict:
        from real_estate.graphflow.report import write_axis
        draft, evidence, violations = write_axis(
            _get_driver(), state["asset_id"], state["asset_card"], axis,
            evidence_k=state.get("evidence_k"),    # None → config.retrieval
            outline=(state.get("outline") or {}).get(axis))   # discourse면 논점, 아니면 None
        return {"sections": {axis: draft},
                "evidence_by_axis": {axis: evidence},
                "mech_audit": {axis: violations}}
    node.__name__ = f"write_{axis}"
    return node


def synthesize_node(state: GraphFlowState) -> dict:
    from real_estate.graphflow.report import synthesize_overview
    overview, index_map, violations = synthesize_overview(
        state["asset_card"], state["sections"])
    return {"overview": overview, "index_map": index_map,
            "mech_audit": {"overview": violations}}


def finalize_node(state: GraphFlowState) -> dict:
    from real_estate.graphflow.report import finalize_report
    report = finalize_report(
        _get_driver(), state["asset_id"], state["sections"], state["overview"],
        state["index_map"], state["evidence_by_axis"], state["mech_audit"],
        out_dir=state["out_dir"])
    return {"report": report}


# ───────────────────────────────────────────────────────────
# 그래프 조립
# ───────────────────────────────────────────────────────────
def build_report_graph(checkpointer=None):
    """LangGraph 조립 — 토폴로지가 곧 흐름의 문서.

        START → seed → doc_intake → tenant ─route─┬─ (static/moderated) → expand ──┐
                       (PDF 있을 때만 동작)         └─ (discourse) perspectives      │
                                                          → discourse_round ⟲ mod    │
                                                          → outline ────────────────┤
                                                                                    ▼
                          [write_tenant ∥ write_esg ∥ write_commercial] → synthesize → finalize

    discourse 경로(STORM/Co-STORM)가 노드로 가시화된다. static/moderated는 expand 노드
    그대로 — route_after_tenant가 mode로 분기, 양 경로는 write로 수렴(diamond).

    doc_intake는 선택적 PDF 입력 축(표제부)이다. pdf_path가 없으면 no-op이라 주소-only
    경로는 불변. 추가 전유부가 필요하면 interrupt로 멈춘다 — 이때만 checkpointer가
    필요하므로, pdf_path를 줄 호출부는 MemorySaver를 넘긴다. checkpointer=None이면
    기존(체크포인터 없는) 컴파일과 동일.
    """
    g = StateGraph(GraphFlowState)
    g.add_node("seed", seed_node)
    g.add_node("doc_intake", doc_intake_node)            # 선택적 PDF 입력 축
    g.add_node("tenant", tenant_node)
    g.add_node("expand", expand_node)
    g.add_node("perspectives", perspectives_node)        # STORM 담론 (discourse mode)
    g.add_node("discourse_round", discourse_round_node)
    g.add_node("moderator", moderator_node)
    g.add_node("outline", outline_node)
    for axis in REPORT_AXES:
        g.add_node(f"write_{axis}", _make_write_node(axis))
    g.add_node("synthesize", synthesize_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "seed")
    g.add_edge("seed", "doc_intake")     # PDF 없으면 doc_intake는 통과(no-op)
    g.add_edge("doc_intake", "tenant")
    # mode 분기: static/moderated → expand, discourse → STORM 담론 서브그래프
    g.add_conditional_edges("tenant", route_after_tenant,
                            {"expand": "expand", "perspectives": "perspectives"})
    # 담론 사이클: perspectives → [discourse_round → moderator]⟲ → outline
    g.add_edge("perspectives", "discourse_round")
    g.add_edge("discourse_round", "moderator")
    g.add_conditional_edges("moderator", route_after_moderator,
                            {"discourse_round": "discourse_round", "outline": "outline"})
    for axis in REPORT_AXES:                 # fan-out: 3축 병렬 (양 경로 수렴)
        g.add_edge("expand", f"write_{axis}")
        g.add_edge("outline", f"write_{axis}")
        g.add_edge(f"write_{axis}", "synthesize")   # fan-in (전 축 완료 대기)
    g.add_edge("synthesize", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer) if checkpointer else g.compile()


def _trace_metadata(asset_id: str) -> dict:
    # LangGraph 루트 trace는 핸들러가 metadata['langfuse_*']로 명명한다. 호출부가
    # run_tracer(trace_name=...)로 감쌌으면 그 값을, 아니면 asset_id로 폴백 —
    # 둘 다 없으면 trace.name이 'LangGraph'로 떨어지므로 항상 채운다.
    md = langchain_trace_metadata()
    md.setdefault("langfuse_trace_name", f"report:{asset_id}")
    md.setdefault("langfuse_session_id", asset_id)
    md.setdefault("langfuse_tags", ["report"])
    return md


def run_report_pipeline(
    driver: Driver,
    *,
    asset_id: str,
    out_dir: str,
    db_path: str = "",
    location=None,
    expand_rounds: int = 0,
    expand_budget: int | None = None,   # None → config.expand.total_query_budget
    evidence_k: int | None = None,
    pdf_path: str | None = None,        # 선택적 표제부 PDF (있으면 doc_intake 가동)
    uploaded_documents: list | None = None,   # 사전 업로드 문서(있으면 interrupt 생략)
    thread_id: str | None = None,       # PDF 경로 재개용 checkpointer thread
) -> GraphFlowState:
    """워크플로우 1회 실행. Langfuse 콜백 자동 부착.

    pdf_path가 없으면 기존과 100% 동일(체크포인터 없이 compile→invoke). pdf_path가
    있으면 MemorySaver로 컴파일해 doc_intake가 추가 문서를 요청할 때 interrupt할 수
    있게 한다 — 반환 state에 '__interrupt__'가 있으면 멈춘 것이고, resume_report_pipeline로
    이어간다. uploaded_documents를 미리 주면 interrupt 없이 통과한다.
    """
    global _driver
    _driver = driver

    # Slack 에러 알림(선택). webhook env 없으면 silent no-op — 기본 동작 무변경.
    # 멱등이므로 매 실행 호출해도 핸들러는 1회만 설치된다.
    from infra.notify import enable_slack_alerting
    enable_slack_alerting()

    from infra.agent.llm import get_langfuse_handler
    callbacks = [h for h in [get_langfuse_handler()] if h is not None]

    initial: GraphFlowState = {
        "asset_id": asset_id, "out_dir": out_dir, "db_path": db_path,
        "location": location, "expand_rounds": expand_rounds,
        "expand_budget": expand_budget, "evidence_k": evidence_k,
        "collected_by": make_run_id("exp"),
    }
    if pdf_path:
        initial["pdf_path"] = pdf_path
    if uploaded_documents:
        initial["uploaded_documents"] = uploaded_documents
    md = _trace_metadata(asset_id)

    # PDF가 없으면 기존 경로 그대로(체크포인터 없음).
    if not pdf_path:
        app = build_report_graph()
        final = app.invoke(initial, config={"callbacks": callbacks, "metadata": md})
        logger.info("workflow 완료 — report=%s",
                    (final.get("report") or {}).get("report_id"))
        return final

    # PDF 경로 — checkpointer로 컴파일(interrupt 가능). thread_id로 재개 식별.
    from langgraph.checkpoint.memory import MemorySaver
    app = build_report_graph(checkpointer=MemorySaver())
    config = {"callbacks": callbacks, "metadata": md,
              "configurable": {"thread_id": thread_id or make_run_id("doc")}}
    final = app.invoke(initial, config=config)
    if final.get("__interrupt__") is not None:
        logger.info("workflow 일시정지(추가 문서 요청) — thread=%s",
                    config["configurable"]["thread_id"])
    else:
        logger.info("workflow 완료 — report=%s",
                    (final.get("report") or {}).get("report_id"))
    # 재개에 필요한 app/config를 동봉(호출부가 resume_report_pipeline에 그대로 넘김).
    final["_resume_handle"] = {"app": app, "config": config}
    return final


def resume_report_pipeline(handle: dict, uploaded_documents: list) -> GraphFlowState:
    """interrupt된 그래프를 사람이 업로드한 문서로 재개.

    handle = run_report_pipeline(pdf_path=...)가 반환한 state['_resume_handle'].
    uploaded_documents = [{"kind","path","unit_id"?}, ...] (사람이 gov.kr에서 받은 전유부).
    """
    from langgraph.types import Command
    app, config = handle["app"], handle["config"]
    final = app.invoke(Command(resume=uploaded_documents), config=config)
    logger.info("workflow 재개 완료 — report=%s",
                (final.get("report") or {}).get("report_id"))
    return final
