"""Per-run JSONL trace logger.

A single TraceLogger is bound to a run via a ContextVar so any module in the
pipeline can grab it without explicit threading. Output goes to
``data/runs/<address_hash>/trace.jsonl`` — one JSON object per line.

Logged event types
------------------
- ``run_start`` / ``run_end``
- ``node_start`` / ``node_end``       (LangGraph nodes in src/graph.py)
- ``planner`` / ``scorer`` / ``report``  (LLM-agent decisions, with input + parsed output)
- ``tool_call``                       (one per tool invocation; includes args, retry diff, output preview)
- ``error``

Retry detection: per (agent, tool) tuple, each subsequent call records the
diff against the previous call's args so it is easy to see how the agent
adjusted parameters between attempts.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# Module-global rather than ContextVar: LangGraph fan-out can dispatch nodes to a
# thread pool, and ContextVars do not propagate across threads by default.
_CURRENT: "TraceLogger | None" = None
_WRITE_LOCK = threading.Lock()

_OUTPUT_PREVIEW_LIMIT = 4000


def _preview(value: Any, limit: int = _OUTPUT_PREVIEW_LIMIT) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


class TraceLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self._tool_history: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self._lock = threading.Lock()

    def log(self, event: str, **payload: Any) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
        }
        entry.update(payload)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def log_tool_call(
        self,
        *,
        agent: str,
        tool: str,
        args: dict[str, Any] | None,
        output: Any = None,
        error: dict[str, Any] | None = None,
        reasoning: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """Log a single tool invocation with retry-aware diff against prior calls."""
        args = dict(args or {})
        with self._lock:
            history = self._tool_history[(agent, tool)]
            retry: dict[str, Any] | None = None
            if history:
                last_args = history[-1]["args"]
                diff: dict[str, Any] = {}
                for k in set(args) | set(last_args):
                    if args.get(k) != last_args.get(k):
                        diff[k] = {"prev": last_args.get(k), "new": args.get(k)}
                retry = {
                    "attempt": len(history) + 1,
                    "same_args_as_prev": not diff,
                    "args_diff_vs_prev": diff or None,
                }
            history.append({"args": args})

        self.log(
            "tool_call",
            agent=agent,
            tool=tool,
            tool_call_id=tool_call_id,
            args=args,
            reasoning=(reasoning or "").strip() or None,
            output_preview=_preview(output),
            error=error,
            retry=retry,
        )


def set_current(logger: TraceLogger | None) -> None:
    global _CURRENT
    _CURRENT = logger


def current() -> TraceLogger | None:
    return _CURRENT


def log_event(event: str, **payload: Any) -> None:
    lg = current()
    if lg is not None:
        lg.log(event, **payload)


def log_tool_call(**kwargs: Any) -> None:
    lg = current()
    if lg is not None:
        lg.log_tool_call(**kwargs)
