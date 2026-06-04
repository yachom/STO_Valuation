"""Validator — 본문에 등장한 [E#] 인용이 실제 evidence와 일치하는지 가벼운 검증.

엄격 검증은 PR-11+에서 강화. v1은 다음만 확인:
    - 각 섹션 본문에 evidence_node_refs가 1개 이상 있는지
    - 본문에 [E#] 패턴이 등장하는지
누락 시 validation_issues에 warning entry 추가 (재실행은 안 함, 표시만).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("agent.validator")

_CITE_PAT = re.compile(r"\[E\d+\]")


def validator_node(state: dict) -> dict:
    from real_estate.report.tracing import emit
    emit("node_start", node="validator",
         sections_count=len(state.get("sections", {})))
    sections = state.get("sections", {})
    issues: list[dict] = []
    for sid, sec in sections.items():
        if not sec.get("evidence_node_refs"):
            issues.append({
                "section_id": sid,
                "axis": sec.get("axis"),
                "kind": "no_evidence",
                "message": "이 섹션은 evidence_node_refs가 비어 있음 (low coverage)",
            })
        if not _CITE_PAT.search(sec.get("body_markdown", "")):
            issues.append({
                "section_id": sid,
                "axis": sec.get("axis"),
                "kind": "no_inline_citation",
                "message": "본문에 [E#] 인용 표시가 없음",
            })
    logger.info("validator issues: %d", len(issues))
    emit("node_end", node="validator", status="ok",
         issues_count=len(issues),
         issues=[{"section_id": i["section_id"], "kind": i["kind"]} for i in issues])
    return {"validation_issues": issues}
