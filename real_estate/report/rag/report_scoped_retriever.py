"""ReportScopedRetriever — 합의된 보고서의 evidence_snippets로만 검색 범위 제한.

원리:
    1. Report.latest()의 모든 섹션에서 evidence_snippets 수집
    2. 한 번 임베딩 (메모리)
    3. retrieve(query)는 query 임베딩 vs cached snippet 임베딩 cosine 정렬
    4. top-k snippets를 GroundedSnippet으로 반환

이걸 통해 QA 단계에선 그래프 전체가 아니라 보고서가 인용한 사실 안에서만
답하게 강제한다 (hallucination floor + scope guarantee).

같은 Retriever 프로토콜을 만족 — agents/qa.py가 그대로 활용.
"""

from __future__ import annotations

import logging

from real_estate.report.domain import Report
from real_estate.report.rag.embedder import embed_one, embed_texts
from real_estate.report.rag.protocol import Axis, GroundedSnippet, Retriever

logger = logging.getLogger("rag.report_scoped")


class ReportScopedRetriever(Retriever):
    def __init__(self, report: Report):
        items: list[dict] = []
        latest = report.latest()
        if latest is None:
            self._items: list[dict] = []
            self._vecs: list[list[float]] = []
            return
        for sec in latest.sections:
            for i, text in enumerate(sec.evidence_snippets or []):
                ref = sec.evidence_node_refs[i] if i < len(sec.evidence_node_refs) else None
                items.append({
                    "text": text,
                    "ref": ref,
                    "axis": sec.axis,
                    "section_id": sec.section_id,
                    "section_title": sec.title,
                })
        self._items = items
        self._vecs = embed_texts([it["text"] for it in items]) if items else []
        logger.info("ReportScopedRetriever: %d evidence snippets indexed", len(items))

    def retrieve(
        self,
        query: str,
        *,
        k: int,
        axis: Axis | None = None,
    ) -> list[GroundedSnippet]:
        if not self._items:
            return []
        qv = embed_one(query)
        scored: list[tuple[float, int]] = []
        for i, vec in enumerate(self._vecs):
            if axis is not None and self._items[i]["axis"] != axis:
                continue
            score = sum(a * b for a, b in zip(qv, vec))  # both unit-normalized
            scored.append((score, i))
        scored.sort(key=lambda x: -x[0])
        out: list[GroundedSnippet] = []
        for score, i in scored[:k]:
            it = self._items[i]
            out.append(GroundedSnippet(
                text=it["text"],
                node_refs=[it["ref"]] if it["ref"] else [],
                score=float(score),
                metadata={
                    "section_id": it["section_id"],
                    "axis": it["axis"],
                    "section_title": it["section_title"],
                },
            ))
        return out
