"""QA Responder — 보고서와 동일 그래프·동일 순회 기반 질의응답 (설계 §11, D6/D7/D11).

흐름:
    ① 앵커: 자산 스코프 지도 요약 + LLM 힌트 추출 → 벡터 매칭 (스코프 N홉 한정)
    ② llm_walk(anchors, 목적=질문) — 적응적 깊이 순회
    ③ 충분성 판정 (코드 + 답변 LLM의 sufficient 플래그)
       부족 → 미니 EXPAND 자동 발동 (질문 구동, 1라운드·검색≤3) → ①~② 재실행 1회
    ④ 답변 + 인용. 이번 질문에서 수집된 근거는 '신규 수집' 라벨.
       여전히 부족 → GAP_AT 마킹 + 범위 밖 정직 응답.

Question/ASKED/ANSWERED_BY/GAP_AT가 그래프에 영구 적재 — QA가 그래프를
살찌우고, GAP은 다음 확장 라운드의 입력 신호가 된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from neo4j import Driver
from pydantic import BaseModel, Field

from real_estate.graphflow.agents.base import BaseAgent
from real_estate.graphflow.composer import Evidence, format_evidence, paths_to_evidence
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.harvester import load_scope_names
from real_estate.graphflow.seeder import ScopeGraph, SeedEdge, _write_edges, seed
from infra.agent.tracing import add_score, emit, node_scope
from real_estate.graphflow.traversal import llm_walk, vector_anchors

logger = logging.getLogger("graphflow.qa")

# 코드 충분성 판정 임계 — 미달이면 미니 EXPAND 발동
_MIN_EVIDENCE = 2
_MIN_TOP_SCORE = 0.12

_MINI_EXPAND_BUDGET = dict(rounds=1, frontier_max=2, total_query_budget=3)


# ───────────────────────────────────────────────────────────
# 스키마
# ───────────────────────────────────────────────────────────
class AnchorHints(BaseModel):
    entity_hints: list[str] = Field(
        description="질문에서 추출한 검색 힌트 2~4개 — 스코프 지도의 엔티티명 또는 "
                    "질문의 핵심 키워드 (예: ['커피전문점', '상권 공실', '임대료'])")


class QAAnswer(BaseModel):
    answer_markdown: str = Field(
        description="답변 (5문장 이내, [E#] 인용 포함). 근거 부족 시 무엇이 부족한지 명시")
    cited_evidence_indexes: list[int] = Field(description="실제 사용한 evidence 번호들")
    sufficient: bool = Field(
        description="제공된 evidence로 질문에 실질적으로 답할 수 있었으면 true")


@dataclass
class Citation:
    ref: str
    text: str
    source: str
    published_at: str | None
    validity_status: str
    is_new: bool                      # 이번 질문의 라이브 확장에서 수집됨


@dataclass
class QAResult:
    question_id: str
    question: str
    answer_markdown: str
    sufficient: bool
    expanded: bool                    # 라이브 확장 발동 여부
    gap: bool                         # 확장 후에도 근거 없음
    citations: list[Citation] = field(default_factory=list)


# ───────────────────────────────────────────────────────────
# 헬퍼
# ───────────────────────────────────────────────────────────
def _scope_map_summary(driver: Driver, asset_id: str, *, cap_per_label: int = 12) -> str:
    """자산 스코프 지도 요약 — 프롬프트용 (전체 그래프가 아니라 스코프만, D11)."""
    by_label: dict[str, list[str]] = {}
    for sn in load_scope_names(driver, asset_id):
        names = by_label.setdefault(sn.label, [])
        if sn.name not in names and len(names) < cap_per_label:
            names.append(sn.name)
    lines = [f"- {label}: {', '.join(names)}" for label, names in sorted(by_label.items())]
    return "\n".join(lines) if lines else "(스코프 비어 있음)"


def _asset_card(driver: Driver, asset_id: str) -> str:
    with driver.session() as s:
        r = s.run(f"MATCH (a:{L.ASSET} {{asset_id: $aid}}) RETURN a.card_text AS c",
                  aid=asset_id).single()
        return (r["c"] if r else None) or asset_id


def _code_sufficient(evidence: list[Evidence]) -> bool:
    return len(evidence) >= _MIN_EVIDENCE and evidence[0].score >= _MIN_TOP_SCORE


# ───────────────────────────────────────────────────────────
# QA Responder
# ───────────────────────────────────────────────────────────
class QAResponder(BaseAgent):
    name = "qa"

    def answer(
        self,
        driver: Driver,
        asset_id: str,
        question: str,
        *,
        allow_expand: bool = True,
        k: int = 10,
        max_hops: int = 3,
    ) -> QAResult:
        question_id = "q_" + uuid4().hex[:10]
        asset_card = _asset_card(driver, asset_id)

        with node_scope("qa", question=question[:80]):
            evidence = self._retrieve(driver, asset_id, question, k=k, max_hops=max_hops)
            expanded = False
            answer: QAAnswer | None = None

            if _code_sufficient(evidence):
                answer = self._generate(asset_card, question, evidence, question_id)

            # 부족 (코드 판정 또는 LLM sufficient=false) → 미니 EXPAND 1회 (D7)
            if allow_expand and (answer is None or not answer.sufficient):
                emit("qa_expand_trigger", question=question[:80],
                     reason="code_insufficient" if answer is None else "llm_insufficient")
                from real_estate.graphflow.expand import run_expand
                run_expand(driver, asset_id=asset_id, collected_by=question_id,
                           purpose=question, **_MINI_EXPAND_BUDGET)
                expanded = True
                evidence = self._retrieve(driver, asset_id, question,
                                          k=k, max_hops=max_hops)
                if evidence:
                    answer = self._generate(asset_card, question, evidence, question_id)

            gap = answer is None or not answer.sufficient
            if answer is None:
                answer = QAAnswer(
                    answer_markdown=(
                        "현재 그래프와 추가 검색으로도 이 질문에 답할 근거를 찾지 "
                        "못했습니다. 보고서 범위 밖 질문으로 기록해 두었으며, 다음 "
                        "데이터 확장 시 우선 수집 대상이 됩니다."),
                    cited_evidence_indexes=[], sufficient=False)

            citations = self._citations(answer, evidence, question_id)
            self._write_question(driver, question_id, asset_id, question,
                                 answer, citations, expanded=expanded, gap=gap)
            emit("qa_done", question_id=question_id, sufficient=answer.sufficient,
                 expanded=expanded, gap=gap, citations=len(citations))
            add_score("qa_sufficient", 1 if answer.sufficient else 0, data_type="BOOLEAN")
            add_score("qa_live_expanded", 1 if expanded else 0, data_type="BOOLEAN")
            add_score("qa_citations", len(citations))
            return QAResult(
                question_id=question_id, question=question,
                answer_markdown=answer.answer_markdown,
                sufficient=answer.sufficient, expanded=expanded, gap=gap,
                citations=citations)

    # ── ①+② 검색: 힌트 → 벡터 앵커 → llm_walk ────────────────
    def _retrieve(self, driver, asset_id, question, *, k, max_hops) -> list[Evidence]:
        scope_summary = _scope_map_summary(driver, asset_id)
        hints: AnchorHints = self.complete(
            system=("질문에서 그래프 검색 힌트를 추출합니다. 스코프 지도에 있는 "
                    "엔티티명을 우선 사용하고, 없으면 질문의 핵심 키워드를 씁니다."),
            user=f"[질문]\n{question}\n\n[자산 스코프 지도]\n{scope_summary}",
            schema=AnchorHints)

        anchor_eids: list[str] = []
        for q in [question] + hints.entity_hints[:4]:
            for eid in vector_anchors(driver, q, k=2,
                                      scope_asset_id=asset_id, scope_hops=max_hops):
                if eid not in anchor_eids:
                    anchor_eids.append(eid)
        anchor_eids = anchor_eids[:5]
        if not anchor_eids:
            return []

        paths = llm_walk(driver, anchor_eids, question, max_hops=max_hops, k=k)
        return paths_to_evidence(driver, paths)

    # ── ④ 답변 생성 ──────────────────────────────────────────
    def _generate(self, asset_card, question, evidence, question_id) -> QAAnswer:
        new_marks = [f"E{ev.index}" for ev in evidence
                     if ev.collected_by == question_id]
        new_note = (f"\n참고: {', '.join(new_marks)}는 이 질문을 위해 방금 수집된 "
                    f"근거입니다(검증 1회). 답변에 인용 시 최신 수집 자료임을 표시하세요."
                    if new_marks else "")
        system = (
            "당신은 상가 자산 평가 그래프 기반 QA 응답자입니다.\n"
            "- 제공된 evidence에서만 답합니다. 추측 금지.\n"
            "- 답변은 5문장 이내, 사용한 근거에 [E#] 인용을 답니다.\n"
            "- 숫자·날짜는 evidence 그대로. 오래된 증거(DECAYED)는 시점을 명시.\n"
            "- evidence로 답이 안 되면 sufficient=false로 두고, 무엇이 부족한지 적습니다."
            + new_note)
        user = (f"[평가 대상 자산]\n{asset_card}\n\n"
                f"[질문]\n{question}\n\n"
                f"[evidence]\n{format_evidence(evidence)}")
        return self.complete(system=system, user=user, schema=QAAnswer)

    @staticmethod
    def _citations(answer, evidence, question_id) -> list[Citation]:
        ev_by_index = {ev.index: ev for ev in evidence}
        out = []
        for i in answer.cited_evidence_indexes:
            ev = ev_by_index.get(i)
            if ev is None:
                continue
            out.append(Citation(
                ref=ev.ref, text=ev.text,
                source=ev.origin.get("press") or ev.source_kind,
                published_at=ev.published_at,
                validity_status=ev.validity_status,
                is_new=ev.collected_by == question_id))
        return out

    # ── Question 그래프 적재 ─────────────────────────────────
    @staticmethod
    def _write_question(driver, question_id, asset_id, question, answer,
                        citations, *, expanded, gap) -> None:
        sg = ScopeGraph()
        sg.add_node(L.QUESTION, question_id, {
            "text": question, "asked_at": datetime.now().isoformat(timespec="seconds"),
            "answered": answer.sufficient, "expanded": expanded,
        })
        edges = [SeedEdge(L.QUESTION, question_id, E.ASKED, L.ASSET, asset_id)]
        for c in citations:
            label, key = c.ref.split(":", 1)
            edges.append(SeedEdge(L.QUESTION, question_id, E.ANSWERED_BY, label, key))
        if gap:
            # 확장까지 했는데도 근거 없음 — 자산에 GAP 마킹 (다음 확장의 입력 신호)
            edges.append(SeedEdge(L.QUESTION, question_id, E.GAP_AT, L.ASSET, asset_id))
        seed(sg, driver)
        _write_edges(driver, edges)
