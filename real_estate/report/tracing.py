"""보고서 생성 1회 run의 상세 trace 수집 + 사이드카 페이로드.

산출물 (보고서 디렉토리 = real_estate/database/DB/reports/{report_id}/):
    run.log                       — human-readable, Python logging FileHandler
    trace.jsonl                   — 슬림 이벤트 (한 줄=JSON, payload_path/sha 참조)
    {version_id}.md               — 보고서 본문
    tool_calls/{call_id}.json     — walk / scout / retriever 풀 페이로드
    llm_calls/{run_id}.json       — 풀 message array + completion + usage
    sql_calls/{call_id}.json      — TracingConnection이 자동 기록한 SQL 풀 페이로드

이벤트 종류:
    pipeline_start / pipeline_end / pipeline_summary
    node_start / node_end            ← node_scope 컨텍스트 매니저 자동 emit
    tool_call                        ← walk:* / scout:* / retriever:*
    sql_query                        ← TracingConnection
    llm_call_start / llm_call_end    ← LLMTraceCallback
    error

contextvar 자동 스탬핑:
    `_current_node`, `_current_axis`가 set돼 있으면 emit이 자동으로 node/axis 부착.
    호출자는 별도로 node= 인자를 넘길 필요 없음 (node_scope/axis_scope 안에서만).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("tracing")

_current_tracer: contextvars.ContextVar["RunTracer | None"] = contextvars.ContextVar(
    "run_tracer", default=None,
)
_current_node: contextvars.ContextVar["str | None"] = contextvars.ContextVar(
    "current_node", default=None,
)
_current_axis: contextvars.ContextVar["str | None"] = contextvars.ContextVar(
    "current_axis", default=None,
)


def make_report_id(prefix: str = "rpt") -> str:
    """타임스탬프 prefix가 박힌 report_id (예: rpt_20260605_120030_8a4f7c1d)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid4().hex[:8]}"


_SIDECAR_DIRS = ("tool_calls", "llm_calls", "sql_calls")


class RunTracer:
    """1회 보고서 생성 run의 trace 수집기. 단일 프로세스 가정."""

    def __init__(self, report_id: str, db_path: Path):
        self._report_id = report_id
        self._run_dir = db_path.parent / "reports" / report_id
        self._run_dir.mkdir(parents=True, exist_ok=True)
        for sub in _SIDECAR_DIRS:
            (self._run_dir / sub).mkdir(exist_ok=True)
        self._jsonl_path = self._run_dir / "trace.jsonl"
        self._log_path = self._run_dir / "run.log"
        self._t0 = time.monotonic()
        self._jsonl_fh = open(self._jsonl_path, "a", encoding="utf-8")
        self._file_handler = self._install_file_handler()
        self._call_seq = 0
        self._call_seq_lock = threading.Lock()
        # tokens_by_node[node][kind] — kind ∈ input_tokens, output_tokens, cache_*
        self._tokens_by_node: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int))
        # tokens_by_model[model][kind]
        self._tokens_by_model: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int))
        self.emit("pipeline_start", report_id=report_id)

    @property
    def report_id(self) -> str:
        return self._report_id

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def next_call_id(self) -> str:
        """monotonic 6-digit call_id (정렬·트레이스 매칭용)."""
        with self._call_seq_lock:
            self._call_seq += 1
            return f"{self._call_seq:06d}"

    def write_payload(
        self, category: str, call_id: str, payload: dict,
    ) -> tuple[str, str]:
        """사이드카 JSON 파일 저장. (relative_path, sha256_short) 반환.

        category는 _SIDECAR_DIRS 중 하나여야 한다 (tool_calls/llm_calls/sql_calls).
        """
        if category not in _SIDECAR_DIRS:
            raise ValueError(f"unknown sidecar category: {category}")
        target = self._run_dir / category / f"{call_id}.json"
        body = json.dumps(payload, ensure_ascii=False, default=str)
        target.write_text(body, encoding="utf-8")
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        rel = f"{category}/{call_id}.json"
        return rel, sha

    def add_tokens(self, node: str | None, model: str | None, usage: dict) -> None:
        """LLM usage dict를 노드별/모델별로 누적."""
        if not usage:
            return
        for k, v in usage.items():
            if not isinstance(v, (int, float)):
                continue
            if node:
                self._tokens_by_node[node][k] += int(v)
            if model:
                self._tokens_by_model[model][k] += int(v)

    def emit(self, event_type: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "elapsed_ms": int((time.monotonic() - self._t0) * 1000),
            "event": event_type,
        }
        # contextvar 자동 스탬핑 — 이미 fields에 있으면 그쪽 우선
        node = _current_node.get()
        axis = _current_axis.get()
        if node is not None and "node" not in fields:
            fields["node"] = node
        if axis is not None and "axis" not in fields:
            fields["axis"] = axis
        record.update(fields)
        self._jsonl_fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._jsonl_fh.flush()
        logger.info("trace [%s] %s", event_type, _short(fields))

    def _install_file_handler(self) -> logging.Handler:
        fh = logging.FileHandler(self._log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        ))
        logging.getLogger().addHandler(fh)
        return fh

    def close(self) -> None:
        # 종료 직전 토큰 합산을 사이드카로
        try:
            self.emit("pipeline_summary",
                      tokens_by_node=dict(
                          (k, dict(v)) for k, v in self._tokens_by_node.items()),
                      tokens_by_model=dict(
                          (k, dict(v)) for k, v in self._tokens_by_model.items()))
        except Exception:
            pass
        self.emit("pipeline_end", elapsed_total_ms=int((time.monotonic() - self._t0) * 1000))
        try:
            self._jsonl_fh.close()
        finally:
            logging.getLogger().removeHandler(self._file_handler)
            try:
                self._file_handler.close()
            except Exception:
                pass


def _short(fields: dict) -> str:
    parts = []
    for k, v in fields.items():
        if isinstance(v, str) and len(v) > 80:
            v = v[:80] + "…"
        if isinstance(v, list) and len(v) > 5:
            v = f"[{len(v)} items]"
        parts.append(f"{k}={v}")
    return " ".join(parts)


def set_tracer(tracer: RunTracer | None) -> None:
    _current_tracer.set(tracer)


def get_tracer() -> RunTracer | None:
    return _current_tracer.get()


def emit(event_type: str, **fields: Any) -> None:
    """현재 tracer가 있으면 emit, 없으면 silent. node/axis는 contextvar 자동 스탬핑."""
    t = _current_tracer.get()
    if t is not None:
        t.emit(event_type, **fields)


def emit_tool_call(
    *,
    tool: str,
    status: str = "ok",
    args: Any = None,
    result: Any = None,
    **extra: Any,
) -> None:
    """walk / scout / retriever 등 모든 tool 호출에 통일된 trace event.

    payload는 사이드카(tool_calls/{call_id}.json)로 저장하고 trace.jsonl엔
    slim event(text_preview + payload_path + sha)만 남긴다. node/axis는
    contextvar에서 자동 스탬핑.
    """
    tracer = _current_tracer.get()
    payload_path = None
    payload_sha = None
    if tracer is not None:
        call_id = tracer.next_call_id()
        payload_path, payload_sha = tracer.write_payload(
            "tool_calls", call_id,
            {"tool": tool, "status": status,
             "args": args, "result": result, **extra},
        )
    preview = ""
    if isinstance(result, dict):
        candidate = (result.get("text") or result.get("error")
                     or result.get("summary") or "")
        preview = str(candidate)[:200]
    elif isinstance(result, str):
        preview = result[:200]
    emit("tool_call", tool=tool, status=status,
         text_preview=preview,
         payload_path=payload_path, payload_sha=payload_sha)


# ───────────────────────────────────────────────────────────
# 노드/축 컨텍스트 매니저
# ───────────────────────────────────────────────────────────
@contextmanager
def node_scope(name: str, **start_fields):
    """LangGraph 노드 진입/이탈 자동 emit + contextvar 스탬핑.

    사용:
        def planner_node(state):
            with node_scope("planner", input_keys=list(state.keys())):
                ...
                return result
    """
    token = _current_node.set(name)
    emit("node_start", node=name, **start_fields)
    try:
        yield
    except Exception as exc:
        emit("error", phase="node", node=name,
             error=type(exc).__name__, message=str(exc)[:300])
        raise
    finally:
        emit("node_end", node=name)
        _current_node.reset(token)


@contextmanager
def axis_scope(axis: str):
    """축(tenant/esg/commercial) contextvar 스탬핑."""
    token = _current_axis.set(axis)
    try:
        yield
    finally:
        _current_axis.reset(token)


# ───────────────────────────────────────────────────────────
# LangChain Callback — LLM 호출 자동 trace + 사이드카
# ───────────────────────────────────────────────────────────
try:
    from langchain_core.callbacks.base import BaseCallbackHandler

    class LLMTraceCallback(BaseCallbackHandler):
        """ChatModel 호출 1건당 start/end 이벤트 + 풀 페이로드 사이드카.

        on_chat_model_start: 메시지 사이드카 작성, llm_call_start emit (node/axis 자동 스탬핑)
        on_llm_end: 응답을 같은 사이드카에 append, usage_tokens roll-up, llm_call_end emit
        on_llm_error: error emit
        """

        def __init__(self):
            self._calls: dict[str, dict] = {}

        def _serialize_messages(self, messages) -> list[dict]:
            flat = messages[0] if messages else []
            out = []
            for m in flat:
                content = getattr(m, "content", None)
                if content is None:
                    content = str(m)
                out.append({
                    "type": getattr(m, "type", m.__class__.__name__),
                    "content": content if isinstance(content, (str, list, dict))
                               else str(content),
                })
            return out

        def on_chat_model_start(self, serialized, messages, *, run_id,
                                parent_run_id=None, metadata=None, **kw):
            tracer = get_tracer()
            try:
                msg_list = self._serialize_messages(messages)
                prompt_chars = sum(
                    len(m["content"]) if isinstance(m["content"], str)
                    else len(json.dumps(m["content"], ensure_ascii=False))
                    for m in msg_list
                )
            except Exception:
                msg_list = []
                prompt_chars = 0
            model = (serialized or {}).get("kwargs", {}).get("model", "?")
            # langchain_anthropic 등은 "model_name" 키로 들어오기도 함
            if model == "?":
                model = (serialized or {}).get("kwargs", {}).get("model_name", "?")
            self._calls[str(run_id)] = {
                "model": model,
                "started_at": time.monotonic(),
                "prompt_chars": prompt_chars,
                "node": _current_node.get(),
                "axis": _current_axis.get(),
            }
            payload_path = None
            payload_sha = None
            if tracer is not None:
                payload_path, payload_sha = tracer.write_payload(
                    "llm_calls", str(run_id),
                    {"phase": "start", "model": model,
                     "metadata": metadata or {}, "messages": msg_list},
                )
            emit("llm_call_start",
                 run_id=str(run_id),
                 parent_run_id=str(parent_run_id) if parent_run_id else None,
                 model=model, prompt_chars=prompt_chars,
                 payload_path=payload_path, payload_sha=payload_sha)

        def on_llm_end(self, response, *, run_id, parent_run_id=None, **kw):
            tracer = get_tracer()
            meta = self._calls.pop(str(run_id), None)
            duration_ms = (
                int((time.monotonic() - meta["started_at"]) * 1000) if meta else None
            )
            stop = None
            usage = {}
            completion_preview = ""
            generations_out: list[dict] = []
            try:
                gens = response.generations
                if gens and gens[0]:
                    info = gens[0][0].generation_info or {}
                    stop = info.get("stop_reason") or info.get("finish_reason")
                    llm_out = response.llm_output or {}
                    usage = llm_out.get("usage") or llm_out.get("token_usage") or {}
                    for g_row in gens:
                        for g in g_row:
                            txt = getattr(g, "text", None) or ""
                            generations_out.append({"text": txt})
                            if not completion_preview and txt:
                                completion_preview = txt[:200]
            except Exception:
                pass
            payload_path = None
            payload_sha = None
            if tracer is not None:
                payload_path, payload_sha = tracer.write_payload(
                    "llm_calls", str(run_id) + "_end",
                    {"phase": "end", "model": (meta or {}).get("model"),
                     "duration_ms": duration_ms, "stop_reason": stop,
                     "usage": usage, "generations": generations_out},
                )
                tracer.add_tokens((meta or {}).get("node"),
                                  (meta or {}).get("model"), usage)
            emit("llm_call_end",
                 run_id=str(run_id),
                 parent_run_id=str(parent_run_id) if parent_run_id else None,
                 model=(meta or {}).get("model"),
                 duration_ms=duration_ms, stop_reason=stop,
                 usage=usage, completion_preview=completion_preview,
                 payload_path=payload_path, payload_sha=payload_sha)

        def on_llm_error(self, error, *, run_id, parent_run_id=None, **kw):
            emit("error", phase="llm", run_id=str(run_id),
                 parent_run_id=str(parent_run_id) if parent_run_id else None,
                 error=type(error).__name__, message=str(error)[:300])

except ImportError:
    class LLMTraceCallback:  # type: ignore
        """LangChain 미설치 환경 fallback (no-op)."""
        pass
