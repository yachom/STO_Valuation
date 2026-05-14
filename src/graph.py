"""LangGraph pipeline.

[metadata] → fan-out([esg], [awareness], [vitality]) → fan-in → [report]
각 노드는 Langfuse 콜백으로 자동 트레이싱된다.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .agents import awareness as awareness_agent
from .agents import esg as esg_agent
from .agents import report as report_agent
from .agents import vitality as vitality_agent
from .metadata.resolver import resolve_address
from .observability import langchain_callbacks
from .state import PipelineState


_CALLBACKS = langchain_callbacks()


def _node_metadata(state: PipelineState) -> dict[str, Any]:
    metadata = resolve_address(state["address"])
    return {"metadata": metadata}


def _node_esg(state: PipelineState) -> dict[str, Any]:
    return {"esg": esg_agent.run(state["metadata"], callbacks=_CALLBACKS)}


def _node_awareness(state: PipelineState) -> dict[str, Any]:
    return {"awareness": awareness_agent.run(state["metadata"], callbacks=_CALLBACKS)}


def _node_vitality(state: PipelineState) -> dict[str, Any]:
    return {"vitality": vitality_agent.run(state["metadata"], callbacks=_CALLBACKS)}


def _node_report(state: PipelineState) -> dict[str, Any]:
    result = report_agent.run(state, callbacks=_CALLBACKS)
    return result


def build_graph():
    g = StateGraph(PipelineState)
    g.add_node("metadata", _node_metadata)
    g.add_node("esg", _node_esg)
    g.add_node("awareness", _node_awareness)
    g.add_node("vitality", _node_vitality)
    g.add_node("report", _node_report)

    g.add_edge(START, "metadata")
    # Fan-out
    g.add_edge("metadata", "esg")
    g.add_edge("metadata", "awareness")
    g.add_edge("metadata", "vitality")
    # Fan-in
    g.add_edge("esg", "report")
    g.add_edge("awareness", "report")
    g.add_edge("vitality", "report")
    g.add_edge("report", END)

    return g.compile()


def run(address: str) -> dict[str, Any]:
    graph = build_graph()
    final = graph.invoke(
        {"address": address},
        config={"callbacks": _CALLBACKS, "run_name": "sto-valuation"},
    )
    return final
