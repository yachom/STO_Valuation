"""Mind Map — Co-STORM 식 동적 concept 트리. (Phase A: insert/seed만)

Co-STORM(EMNLP 2024, Jiang et al.)이 사용한 hierarchical mind map의 우리 도메인 어댑트.
원 논문은 일반 정보 수집용이라 perspective(=expert role)도 동적이지만, 본 평가는
도메인상 3축(tenant/esg/commercial)이 root의 1단 자식으로 고정되고 그 아래만 동적.

Phase A 범위:
    - Concept/Evidence dataclass
    - MindMap.add_sub_concept / add_evidence_to / set_focus_question
    - to_dict() — state 직렬화
    - pretty()  — 디버그 출력
    - 향후 Phase B에서 reorganize/cleanup, LLM 기반 Insert 결정 추가.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

Axis = Literal["tenant", "esg", "commercial"]
_AXES: tuple[Axis, ...] = ("tenant", "esg", "commercial")
_AXIS_LABELS = {"tenant": "테넌트 가치", "esg": "ESG 가치", "commercial": "상권 가치"}


@dataclass
class Evidence:
    ev_id: str
    text: str
    ref: str
    source: str            # "prefetch:<method>" | "hitl" | "qa" | "scout:<m>" | "retriever:..."
    axis: Axis | None = None


@dataclass
class Concept:
    concept_id: str
    parent_id: str | None
    label: str
    axis: Axis | None
    evidence_ids: list[str] = field(default_factory=list)
    focus_question: str | None = None
    origin: str = "planner"  # "planner" | "hitl" | "qa" | "replan_N"
    children: list[str] = field(default_factory=list)


class MindMap:
    """3축 root + 동적 sub-concept 트리. dict 직렬화 가능."""

    def __init__(self) -> None:
        self.concepts: dict[str, Concept] = {}
        # root
        self._add(Concept(concept_id="root", parent_id=None,
                          label="자산 평가", axis=None))
        # 3 axis roots (고정)
        for ax in _AXES:
            self._add(Concept(concept_id=ax, parent_id="root",
                              label=_AXIS_LABELS[ax], axis=ax))

    # ─── 내부 ───
    def _add(self, c: Concept) -> None:
        self.concepts[c.concept_id] = c
        if c.parent_id and c.parent_id in self.concepts:
            self.concepts[c.parent_id].children.append(c.concept_id)

    # ─── 공개 ops ───
    def add_evidence_to(self, concept_id: str, ev_id: str) -> None:
        if concept_id in self.concepts:
            self.concepts[concept_id].evidence_ids.append(ev_id)

    def add_sub_concept(
        self, *, parent_id: str, label: str, focus_question: str | None,
        origin: str, axis: Axis | None,
    ) -> str:
        """parent 아래 새 concept 생성, concept_id 반환."""
        if parent_id not in self.concepts:
            raise ValueError(f"parent '{parent_id}' not in mind map")
        cid = f"{parent_id}.{uuid.uuid4().hex[:6]}"
        self._add(Concept(
            concept_id=cid, parent_id=parent_id, label=label,
            axis=axis, focus_question=focus_question, origin=origin,
        ))
        return cid

    def set_focus_question(self, concept_id: str, fq: str) -> None:
        if concept_id in self.concepts:
            self.concepts[concept_id].focus_question = fq

    # ─── 직렬화/표시 ───
    def to_dict(self) -> dict[str, dict]:
        return {cid: asdict(c) for cid, c in self.concepts.items()}

    @classmethod
    def from_dict(cls, d: dict[str, dict]) -> MindMap:
        """state에서 복원. Phase B replan에서 사용."""
        m = cls.__new__(cls)
        m.concepts = {}
        # 부모-자식 순서 무관하게 1회 채우고, children 리스트는 dict에 이미 들어있음
        for cid, payload in d.items():
            m.concepts[cid] = Concept(**payload)
        return m

    def pretty(self, evidence_pool: dict | None = None,
               max_ev_show: int = 3) -> str:
        lines: list[str] = []

        def _walk(cid: str, depth: int) -> None:
            c = self.concepts.get(cid)
            if c is None:
                return
            indent = "  " * depth
            ev_n = len(c.evidence_ids)
            tag = f"[{ev_n} ev]" if ev_n else ""
            head = f"{indent}- {c.label} {tag}".rstrip()
            tail = f" (axis={c.axis or '-'}, origin={c.origin})"
            lines.append(head + tail)
            if c.focus_question:
                lines.append(f"{indent}    Q: {c.focus_question}")
            if evidence_pool:
                for ev_id in c.evidence_ids[:max_ev_show]:
                    ev = evidence_pool.get(ev_id, {})
                    text = (ev.get("text") or "")[:80]
                    lines.append(f"{indent}    · [{ev_id}] {text}")
                if ev_n > max_ev_show:
                    lines.append(f"{indent}    · ... ({ev_n - max_ev_show} more)")
            for child_id in c.children:
                _walk(child_id, depth + 1)

        _walk("root", 0)
        return "\n".join(lines)
