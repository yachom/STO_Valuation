"""LangGraph state schema (v2)."""

from __future__ import annotations

from typing import Any, TypedDict


class PipelineState(TypedDict, total=False):
    address: str
    metadata: dict[str, Any]
    news: dict[str, Any]          # {fetch, tagged_count, graph_summary, graph(KG object — not serialized)}
    esg: dict[str, Any]
    awareness: dict[str, Any]
    vitality: dict[str, Any]
    composite: dict[str, Any]
    report_markdown: str
    errors: list[str]
