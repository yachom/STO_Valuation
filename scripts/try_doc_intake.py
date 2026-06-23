"""graphflow 선택적 PDF 입력 축 시험 — doc_intake interrupt/resume (HITL).

표제부(집합건축물대장) PDF 1개 → 파싱·추출 → doc_requester 판정 → 추가 전유부가
필요하면 LangGraph가 interrupt로 멈춰 사람에게 요청(gov.kr URL + 호수). 사람이 전유부를
업로드(시뮬레이션)하면 Command(resume=...)로 그래프를 이어간다.

전체 보고서 그래프(seed→tenant→…)는 Neo4j·여러 외부 키가 필요하므로, 이 시험은
**실제 doc_intake_node**를 최소 그래프(START→doc_intake→END)에 꽂아 interrupt/resume만
격리 시연한다. 프로덕션 build_report_graph도 동일 노드를 동일 위치(seed→doc_intake→
tenant)에 쓰고, checkpointer를 주면 같은 지점에서 interrupt한다(끝에서 컴파일 확인).

사용:
    # LLM 판정(클라우드 폴백 — 로컬 vLLM 미가동 시):
    AGENT_TIER_DOC_REQUESTER=sonnet python -m scripts.try_doc_intake
    # 키 없이 결정적 keyword 휴리스틱으로:
    python -m scripts.try_doc_intake --no-llm
    # 다른 표제부 PDF로:
    python -m scripts.try_doc_intake --pdf /path/to/표제부.pdf

env:
- doc_requester tier 기본값은 'local'(openai_compat/vLLM, config/models.json). vLLM 서버가
  없으면 `AGENT_TIER_DOC_REQUESTER=sonnet`로 클라우드 폴백하라(이 스크립트가 안내).
- ANTHROPIC/OPENAI 키가 전혀 없으면 LLM 호출이 실패하고 doc_requester가 자동으로
  keyword 휴리스틱으로 폴백한다(부재 데드락 없음).
- .env는 프로젝트 루트(infra/의 상위)에서 로드. worktree에 .env가 없으면 --env-file로
  메인 트리 .env를 가리킬 수 있다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from infra.env import load_env

# 위반건축물 샘플(표제부/전유부)은 gitignored downloads/에만 있다 — 절대경로로 참조.
_DEFAULT_PDF = ("/Users/baemingyu/project/STO_valuation/downloads/gov24/"
                "표제부_위반건축물.pdf")
_SAMPLE_JEONYU = ("/Users/baemingyu/project/STO_valuation/downloads/gov24/"
                  "전유부_위반건축물.pdf")


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(description="doc_intake interrupt/resume 시험")
    ap.add_argument("--pdf", default=_DEFAULT_PDF, help="표제부(집합건축물대장) PDF 경로")
    ap.add_argument("--no-llm", action="store_true",
                    help="LLM 없이 결정적 keyword 휴리스틱으로 판정")
    ap.add_argument("--env-file", default=None,
                    help=".env 경로(worktree에 .env가 없으면 메인 트리 .env 지정)")
    args = ap.parse_args()

    load_env(args.env_file) if args.env_file else load_env()

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"ERROR: PDF 없음: {pdf}", file=sys.stderr)
        return 2

    import os
    if not args.no_llm and os.environ.get("AGENT_TIER_DOC_REQUESTER") is None:
        print("힌트: doc_requester 기본 tier는 'local'(vLLM). 로컬 서버가 없으면 "
              "AGENT_TIER_DOC_REQUESTER=sonnet 로 클라우드 폴백하세요. "
              "키가 전혀 없으면 keyword 휴리스틱으로 자동 폴백합니다.")

    # ── 1) 파싱 + 추출 ──────────────────────────────────────────────────────
    from real_estate.external.gov24.extract import extract_from_clean_pdf
    from real_estate.external.gov24.parser import parse_gov24_pdf

    _print_header(f"1) 표제부 파싱/추출 — {pdf.name}")
    parsed = parse_gov24_pdf(pdf)
    extracted = extract_from_clean_pdf(parsed)
    print(f"페이지 {parsed.page_count} | 문서유형 {extracted.document_type} | "
          f"건물 '{extracted.name}' (ID {extracted.building_id})")
    print(f"도로명주소: {extracted.road_address}")
    print(f"건축물 현황 행 {len(extracted.building_rows)}개 | 변동사항 {len(extracted.changes)}개")
    risky = [c for c in extracted.changes
             if any(k in c.description for k in ("위반", "무단", "대수선", "용도변경"))]
    print(f"리스크 변동 표기 {len(risky)}건 (예: "
          f"{risky[0].description[:60] if risky else '없음'}…)")

    # ── 2) doc_requester 판정 (LLM 또는 휴리스틱) ────────────────────────────
    from real_estate.graphflow.agents.doc_requester import DocRequester

    _print_header("2) doc_requester 판정 — 추가로 받을 전유부 호수")
    result = DocRequester().decide(parsed, extracted, min_priority="medium",
                                   use_llm=not args.no_llm)
    print(f"decided_by={result.decided_by} | needs_more={result.needs_more_documents}")
    for r in result.requests:
        print(f"  · {r.unit_id} [{r.priority}] {r.reason[:80]}")
    if not result.requests:
        print("  (리스크 호 없음 — interrupt 없이 통과)")

    # ── 3) 그래프 interrupt → resume ────────────────────────────────────────
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    from real_estate.graphflow.state import GraphFlowState
    from real_estate.graphflow.workflow import doc_intake_node

    g = StateGraph(GraphFlowState)
    g.add_node("doc_intake", doc_intake_node)
    g.add_edge(START, "doc_intake")
    g.add_edge("doc_intake", END)
    app = g.compile(checkpointer=MemorySaver())   # interrupt는 checkpointer 필요
    config = {"configurable": {"thread_id": "try-doc-intake"}}

    _print_header("3) 그래프 실행 — PDF 제공 → interrupt 예상")
    state1 = app.invoke({"asset_id": "TRY", "pdf_path": str(pdf)}, config=config)
    intr = state1.get("__interrupt__")
    if intr is None:
        print("interrupt 없음 — 추가 문서가 필요 없다고 판정됨(정상 종료).")
        return 0
    payload = intr[0].value
    print(">>> 그래프가 멈췄습니다 (HITL). 사람에게 보낼 요청:")
    print(payload["message"])
    print(f"\n요청 호수: {payload['requested_units']}")

    _print_header("4) 사람이 전유부 업로드 → Command(resume=...)로 재개")
    uploaded = [{"kind": "집합건축물대장(전유부)", "path": _SAMPLE_JEONYU,
                 "unit_id": payload["requested_units"][0]}]
    print(f"업로드(시뮬레이션): {uploaded[0]['unit_id']} 전유부 = {Path(_SAMPLE_JEONYU).name}")
    state2 = app.invoke(Command(resume=uploaded), config=config)
    still = state2.get("__interrupt__") is not None
    have = [d.get("unit_id") for d in (state2.get("uploaded_documents") or [])]
    print(f"재개 후 interrupt 잔존? {still}")
    print(f"uploaded_documents: {have}")
    print(f"pending_doc_requests: {state2.get('pending_doc_requests')}")
    di = state2.get("document_intake") or {}
    print(f"document_intake.decided_by={di.get('decided_by')} | "
          f"needs_more={di.get('needs_more_documents')}")

    # ── 5) 프로덕션 그래프도 동일 노드를 checkpointer로 컴파일 가능 ───────────
    _print_header("5) 프로덕션 build_report_graph(checkpointer) 컴파일 확인")
    from real_estate.graphflow.workflow import build_report_graph
    prod = build_report_graph(checkpointer=MemorySaver())
    nodes = list(prod.get_graph().nodes.keys())
    print(f"컴파일 OK | doc_intake 노드 존재: {'doc_intake' in nodes} | "
          f"엣지 seed→doc_intake→tenant")
    print("\n(주소-only 경로는 pdf_path 없이 build_report_graph()로 컴파일 — 불변)")
    print("\n=== try_doc_intake 완료 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
