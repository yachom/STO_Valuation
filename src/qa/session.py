"""QA 세션 컨테이너.

v2 산출물(report.md / result.json / knowledge_graph.json)을 한 번 로딩해
모든 specialist가 공유한다. metadata만 가벼운 형태로 노출.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..kg.graph_store import KnowledgeGraph


@dataclass
class QASession:
    report_path: str
    result_path: str
    kg_path: str
    report_text: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    kg: KnowledgeGraph | None = None
    history: list[dict[str, str]] = field(default_factory=list)  # [{role, content}]

    @classmethod
    def load(cls, report_path: str, result_path: str, kg_path: str) -> "QASession":
        s = cls(report_path=report_path, result_path=result_path, kg_path=kg_path)
        s.report_text = Path(report_path).read_text(encoding="utf-8")
        s.result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        kg = KnowledgeGraph()
        if Path(kg_path).exists():
            kg.load(kg_path)
        s.kg = kg
        return s

    # ----- 컨텍스트 추출 헬퍼 -----

    def metadata(self) -> dict[str, Any]:
        return self.result.get("metadata") or {}

    def axis(self, key: str) -> dict[str, Any]:
        return (self.result.get(key.lower()) or {})

    def composite(self) -> dict[str, Any]:
        return self.result.get("composite") or {}

    def report_excerpt(self, max_chars: int = 18000) -> str:
        if len(self.report_text) <= max_chars:
            return self.report_text
        # 앞쪽 + 뒤쪽 보존
        head = self.report_text[: max_chars - 2000]
        tail = self.report_text[-2000:]
        return head + "\n\n...[중략]...\n\n" + tail

    def address_label(self) -> str:
        addr = self.metadata().get("address") or {}
        return addr.get("building_name") or addr.get("road") or self.result.get("address") or "대상 자산"

    def append_turn(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def history_excerpt(self, max_turns: int = 6) -> str:
        if not self.history:
            return ""
        recent = self.history[-max_turns:]
        return "\n".join(f"[{t['role']}] {t['content']}" for t in recent)
