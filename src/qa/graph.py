"""QA LangGraph: router → specialists fan-out → synthesizer."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..observability import langchain_callbacks
from .router import route
from .session import QASession
from .specialists import (
    axis_deep_dive,
    composite_recalc,
    fallback,
    graph_query,
    live_tool,
    report_analyst,
)
from .synthesizer import synthesize

_CALLBACKS = langchain_callbacks()

_SPECIALIST_FNS = {
    "report_analyst": report_analyst.run,
    "graph_query": graph_query.run,
    "axis_deep_dive": axis_deep_dive.run,
    "live_tool": live_tool.run,
    "composite_recalc": composite_recalc.run,
    "fallback": fallback.run,
}


def answer(question: str, session: QASession) -> dict[str, Any]:
    routing = route(question, session, callbacks=_CALLBACKS)
    picks = routing.get("specialists") or ["fallback"]

    answers: dict[str, str] = {}

    def _run(name: str) -> tuple[str, str]:
        try:
            fn = _SPECIALIST_FNS[name]
            return name, fn(question, session, callbacks=_CALLBACKS)
        except Exception as exc:
            return name, f"(전문가 '{name}' 실패: {type(exc).__name__}: {exc})"

    if routing.get("parallel") and len(picks) > 1:
        with ThreadPoolExecutor(max_workers=min(len(picks), 4)) as ex:
            futures = [ex.submit(_run, n) for n in picks]
            for f in as_completed(futures):
                name, ans = f.result()
                answers[name] = ans
    else:
        for n in picks:
            name, ans = _run(n)
            answers[name] = ans

    final = synthesize(question, answers, session, router_meta=routing, callbacks=_CALLBACKS)

    session.append_turn("user", question)
    session.append_turn("assistant", final)

    return {
        "answer": final,
        "routing": routing,
        "specialist_answers": answers,
    }
