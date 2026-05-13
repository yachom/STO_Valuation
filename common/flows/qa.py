"""QA LangGraph: Analyzer → Retriever → Synthesizer → Verifier loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QAState(BaseModel):
    question: str
    scope_id: str
    retries: int = 0
    max_retries: int = 2
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    answer: str | None = None
    verified: bool = False


def build_qa_graph(
    *,
    analyzer: Any,
    retriever: Any,
    synthesizer: Any,
    verifier: Any,
    max_retries: int = 2,
) -> Any:
    raise NotImplementedError
