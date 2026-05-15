"""LangGraph v2 - Planner/Retriever/Scorer 축 서브그래프 + 뉴스 KG 트랙.

흐름:
  START
   └─ metadata
        ├─ news (fetch → tag → KG build)        ┐
        └─ (fan-out 3 axes - 단, 각 축은 news 결과의 KG를 참조)
            ├─ esg_subgraph
            ├─ awareness_subgraph
            └─ vitality_subgraph
              └─ fan-in → report → END

LangGraph 노드 의존성:
  news_pipeline은 metadata 직후 시작되고, 각 축은 news 완료 후 시작 (KG 사용).
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .agents import report as report_agent
from .agents.axis_subgraph import run_axis
from .metadata.resolver import resolve_address
from .news.pipeline import run as run_news
from .observability import langchain_callbacks
from .state import PipelineState
from . import trace_logger
from .web.run_store import run_dir

_CALLBACKS = langchain_callbacks()

# Module-level shared knowledge graph for a single run.
_RUN_CACHE: dict[str, Any] = {}


def _wrap_node(name: str, fn):
    def wrapped(state: PipelineState) -> dict[str, Any]:
        trace_logger.log_event("node_start", node=name)
        try:
            out = fn(state)
        except Exception as exc:
            trace_logger.log_event(
                "error",
                node=name,
                exception_type=type(exc).__name__,
                message=str(exc)[:500],
            )
            raise
        # Log a compact summary of the node output to avoid bloating the trace.
        summary: dict[str, Any] = {}
        for k, v in (out or {}).items():
            if isinstance(v, dict):
                summary[k] = {kk: (type(vv).__name__) for kk, vv in v.items()}
            elif isinstance(v, list):
                summary[k] = f"list(len={len(v)})"
            else:
                summary[k] = type(v).__name__
        trace_logger.log_event("node_end", node=name, output_keys=summary)
        return out
    return wrapped


def _node_metadata(state: PipelineState) -> dict[str, Any]:
    md = resolve_address(state["address"])
    return {"metadata": md}


def _node_news(state: PipelineState) -> dict[str, Any]:
    payload = run_news(state["metadata"], callbacks=_CALLBACKS)
    kg = payload.pop("graph")  # KG object kept out of state dict (not JSON-friendly)
    _RUN_CACHE["kg"] = kg
    return {"news": payload}


def _node_esg(state: PipelineState) -> dict[str, Any]:
    return {"esg": run_axis("ESG", "ESG (환경·사회·거버넌스)", state["metadata"], _RUN_CACHE.get("kg"), callbacks=_CALLBACKS)}


def _node_awareness(state: PipelineState) -> dict[str, Any]:
    return {"awareness": run_axis("AWARENESS", "인지도", state["metadata"], _RUN_CACHE.get("kg"), callbacks=_CALLBACKS)}


def _node_vitality(state: PipelineState) -> dict[str, Any]:
    return {"vitality": run_axis("VITALITY", "상권·입지 활성도", state["metadata"], _RUN_CACHE.get("kg"), callbacks=_CALLBACKS)}


def _node_report(state: PipelineState) -> dict[str, Any]:
    kg = _RUN_CACHE.get("kg")
    kg_insights = {}
    if kg is not None:
        kg_insights = {
            "summary": kg.summary(),
            "axis_evidence": {
                "ESG": kg.axis_evidence("ESG"),
                "AWARENESS": kg.axis_evidence("AWARENESS"),
                "VITALITY": kg.axis_evidence("VITALITY"),
            },
        }
    enriched = {**state, "kg_insights": kg_insights}
    return report_agent.run(enriched, callbacks=_CALLBACKS)


def build_graph():
    g = StateGraph(PipelineState)
    g.add_node("metadata", _wrap_node("metadata", _node_metadata))
    g.add_node("news", _wrap_node("news", _node_news))
    g.add_node("esg", _wrap_node("esg", _node_esg))
    g.add_node("awareness", _wrap_node("awareness", _node_awareness))
    g.add_node("vitality", _wrap_node("vitality", _node_vitality))
    g.add_node("report", _wrap_node("report", _node_report))

    g.add_edge(START, "metadata")
    g.add_edge("metadata", "news")
    # news 완료 후 3축 fan-out
    g.add_edge("news", "esg")
    g.add_edge("news", "awareness")
    g.add_edge("news", "vitality")
    # fan-in
    g.add_edge("esg", "report")
    g.add_edge("awareness", "report")
    g.add_edge("vitality", "report")
    g.add_edge("report", END)
    return g.compile()


def run(address: str) -> dict[str, Any]:
    _RUN_CACHE.clear()

    # Per-run JSONL trace at data/runs/<hash>/trace.jsonl. Same directory used by run_store.save_run.
    rdir = run_dir(address)
    logger = trace_logger.TraceLogger(rdir / "trace.jsonl")
    trace_logger.set_current(logger)
    logger.log("run_start", address=address, run_dir=str(rdir))

    graph = build_graph()
    try:
        final = graph.invoke(
            {"address": address},
            config={"callbacks": _CALLBACKS, "run_name": "sto-valuation-v2"},
        )
    except Exception as exc:
        logger.log("run_end", status="error", exception_type=type(exc).__name__, message=str(exc)[:500])
        trace_logger.set_current(None)
        raise
    logger.log("run_end", status="ok")
    trace_logger.set_current(None)
    # Attach the KG as serialized JSON dict (for callers).
    kg = _RUN_CACHE.get("kg")
    if kg is not None:
        try:
            import networkx as nx
            final["knowledge_graph"] = nx.readwrite.json_graph.node_link_data(kg.g, edges="links")
        except Exception:
            pass
        try:
            kg.save("data/knowledge_graph.json")
            final["knowledge_graph_path"] = "data/knowledge_graph.json"
        except Exception:
            pass
    return final
