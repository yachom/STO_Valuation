"""트레이싱 — 로컬 사이드카(자급) + Langfuse(클라우드) 통합 진입점.

graphflow의 모든 에이전트/툴/LLM 호출은 이 모듈만 거치면 두 곳에 동시 추적된다.
에이전트·툴 코드는 langfuse를 **직접 import하지 않는다** — `observe` 데코레이터와
`emit_tool_call`/`node_scope`만 graphflow.tracing에서 가져다 쓴다.

두 트레이싱 층:
    1. Langfuse(클라우드): LLM 호출은 get_llm의 CallbackHandler가 자동 추적.
       에이전트/툴 span은 `observe(as_type=...)` 데코레이터로 그룹핑.
    2. 로컬 사이드카(reports/{run_id}/): trace.jsonl + 풀 페이로드. RunTracer가
       set돼 있을 때만 기록(없으면 silent) — Langfuse는 키만 있으면 항상.

산출물 (RunTracer 활성 시, real_estate/database/DB/reports/{run_id}/):
    run.log / trace.jsonl / tool_calls|llm_calls|sql_calls/{id}.json

contextvar 자동 스탬핑: _current_node / _current_axis가 set이면 emit이 자동 부착.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("graphflow.tracing")

_current_tracer: contextvars.ContextVar["RunTracer | None"] = contextvars.ContextVar(
    "run_tracer", default=None)
_current_node: contextvars.ContextVar["str | None"] = contextvars.ContextVar(
    "current_node", default=None)
_current_axis: contextvars.ContextVar["str | None"] = contextvars.ContextVar(
    "current_axis", default=None)
# trace_context가 건 trace 속성 — LangGraph invoke의 langfuse metadata 변환용.
_current_trace_attrs: contextvars.ContextVar["dict | None"] = contextvars.ContextVar(
    "trace_attrs", default=None)

_SIDECAR_DIRS = ("tool_calls", "llm_calls", "sql_calls")


def make_run_id(prefix: str = "rpt") -> str:
    """타임스탬프 prefix가 박힌 run_id (예: rpt_20260611_120030_8a4f7c1d).

    prefix로 run 종류 구분: rpt(보고서) / qa(질의응답) / exp(확장).
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid4().hex[:8]}"


class RunTracer:
    """1회 run의 로컬 사이드카 trace 수집기. 단일 프로세스 가정."""

    def __init__(self, run_id: str, db_path: Path):
        self._run_id = run_id
        self._run_dir = Path(db_path).parent / "reports" / run_id
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
        self._tokens_by_node: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._tokens_by_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.emit("pipeline_start", run_id=run_id)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def next_call_id(self) -> str:
        with self._call_seq_lock:
            self._call_seq += 1
            return f"{self._call_seq:06d}"

    def write_payload(self, category: str, call_id: str, payload: dict) -> tuple[str, str]:
        if category not in _SIDECAR_DIRS:
            raise ValueError(f"unknown sidecar category: {category}")
        target = self._run_dir / category / f"{call_id}.json"
        body = json.dumps(payload, ensure_ascii=False, default=str)
        target.write_text(body, encoding="utf-8")
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        return f"{category}/{call_id}.json", sha

    def add_tokens(self, node: str | None, model: str | None, usage: dict) -> None:
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
        node, axis = _current_node.get(), _current_axis.get()
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
        fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        return fh

    def close(self) -> None:
        try:
            self.emit("pipeline_summary",
                      tokens_by_node={k: dict(v) for k, v in self._tokens_by_node.items()},
                      tokens_by_model={k: dict(v) for k, v in self._tokens_by_model.items()})
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


@contextmanager
def run_tracer(run_id: str, db_path: str | Path, *,
               session_id: str | None = None, trace_name: str | None = None,
               tags: list[str] | None = None, metadata: dict | None = None):
    """RunTracer를 set + Langfuse trace 속성 전파 + 종료 시 close/flush.

    session_id: Langfuse Sessions 그룹핑 (자산별 추천 — asset_id).
    trace_name: 대시보드 trace 이름 (예: 'report:강남대로396').
    tags: 분류 태그 (report/qa/revise/expand 등).
    metadata: 필터용 부가정보 (asset_id, rounds 등). 값은 자동 str화.
    """
    tracer = RunTracer(run_id, Path(db_path))
    token = _current_tracer.set(tracer)
    try:
        with trace_context(name=trace_name or run_id, session_id=session_id,
                           tags=tags, metadata=metadata):
            yield tracer
    finally:
        tracer.close()
        _current_tracer.reset(token)
        flush_langfuse()


@contextmanager
def trace_context(*, name: str | None = None, session_id: str | None = None,
                  tags: list[str] | None = None, metadata: dict | None = None):
    """Langfuse trace-level 속성을 이 컨텍스트 내 모든 span에 전파. 키 없으면 no-op.

    LLM 호출의 CallbackHandler가 만드는 span이 이 속성(session/tags/metadata)을
    상속한다 → 대시보드에서 자산별·단계별 집계. 속성은 contextvar에도 저장돼
    LangGraph 경로가 langchain_trace_metadata로 꺼내 쓴다(아래 함수 참고).
    """
    token = _current_trace_attrs.set(
        {"name": name, "session_id": session_id, "tags": tags, "metadata": metadata})
    try:
        try:
            from langfuse import propagate_attributes
        except Exception:
            yield
            return
        kw: dict = {"as_baggage": True}
        if name:
            kw["trace_name"] = name
        if session_id:
            kw["session_id"] = session_id
        if tags:
            kw["tags"] = list(tags)
        if metadata:
            kw["metadata"] = {k: str(v) for k, v in metadata.items() if v is not None}
        try:
            with propagate_attributes(**kw):
                yield
        except Exception as e:
            logger.debug("langfuse trace_context skip: %s", e)
            yield
    finally:
        _current_trace_attrs.reset(token)


def langchain_trace_metadata() -> dict:
    """현재 trace_context 속성 → LangChain config의 `metadata` dict (langfuse_* 키).

    langfuse v4 LangChain CallbackHandler는 trace 이름/세션/태그를 runnable의
    metadata['langfuse_trace_name'/'langfuse_session_id'/'langfuse_tags']에서 읽는다
    (propagate_attributes의 OTel baggage가 아니라). LangGraph는 app.invoke가 만드는
    루트 span을 핸들러가 명명하므로, 이 dict를 invoke config의 metadata로 넘겨야
    trace.name이 run_tracer(trace_name=...)대로 잡힌다(안 넘기면 'LangGraph'로 떨어짐).

    활성 trace_context가 없으면 빈 dict — 호출부가 폴백 이름을 정한다.
    """
    a = _current_trace_attrs.get() or {}
    md: dict = {}
    if a.get("name"):
        md["langfuse_trace_name"] = a["name"]
    if a.get("session_id"):
        md["langfuse_session_id"] = a["session_id"]
    if a.get("tags"):
        md["langfuse_tags"] = list(a["tags"])
    for k, v in (a.get("metadata") or {}).items():
        if v is not None:
            md[k] = str(v)
    return md


def add_score(name: str, value, *, data_type: str | None = None,
              comment: str | None = None) -> None:
    """현재 Langfuse trace에 품질 점수 부착 (Auditor 통과/novelty 등). graceful no-op."""
    try:
        from langfuse import get_client
        get_client().score_current_trace(
            name=name, value=value, data_type=data_type, comment=comment)
    except Exception as e:
        logger.debug("langfuse score skip: %s", e)


def emit(event_type: str, **fields: Any) -> None:
    """현재 tracer가 있으면 사이드카 emit, 없으면 silent. node/axis 자동 스탬핑."""
    t = _current_tracer.get()
    if t is not None:
        t.emit(event_type, **fields)


def emit_tool_call(*, tool: str, status: str = "ok", args: Any = None,
                   result: Any = None, **extra: Any) -> None:
    """모든 tool 호출의 통일 trace event. 풀 페이로드는 사이드카, slim event만 jsonl."""
    tracer = _current_tracer.get()
    payload_path = payload_sha = None
    if tracer is not None:
        call_id = tracer.next_call_id()
        payload_path, payload_sha = tracer.write_payload(
            "tool_calls", call_id,
            {"tool": tool, "status": status, "args": args, "result": result, **extra})
    preview = ""
    if isinstance(result, dict):
        preview = str(result.get("text") or result.get("error") or result.get("summary") or "")[:200]
    elif isinstance(result, str):
        preview = result[:200]
    emit("tool_call", tool=tool, status=status, text_preview=preview,
         payload_path=payload_path, payload_sha=payload_sha)


@contextmanager
def node_scope(name: str, **start_fields):
    """노드 진입/이탈 자동 emit + contextvar 스탬핑."""
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
# Langfuse 통합 — observe 데코레이터 thin wrapper (에이전트/툴이 직접 import 안 하게)
# ───────────────────────────────────────────────────────────
def _resolve_observe():
    try:
        from langfuse import observe as _obs
        return _obs
    except Exception:                          # langfuse 미설치/구버전 → no-op
        def _noop(func=None, **kw):
            def deco(fn):
                return fn
            return deco(func) if callable(func) else deco
        return _noop


_observe_impl = _resolve_observe()


def observe(name: str | None = None, *, as_type: str = "span"):
    """Langfuse span 데코레이터. 에이전트/툴은 graphflow.tracing.observe만 쓴다.

    as_type: 'agent' | 'tool' | 'span' | 'generation' 등. Langfuse 키 없으면 no-op.
    LLM 호출 자체는 get_llm의 CallbackHandler가 별도로 추적 — 이 span은 그 상위
    그룹(에이전트/툴 단위)을 만든다.
    """
    return _observe_impl(name=name, as_type=as_type)


def flush_langfuse() -> None:
    """버퍼된 Langfuse 이벤트 전송 (프로세스 종료 전/run 종료 시)."""
    try:
        from langfuse import get_client
        get_client().flush()
    except Exception as e:
        logger.debug("langfuse flush skip: %s", e)


# ───────────────────────────────────────────────────────────
# LangChain Callback — LLM 호출 자동 trace + 사이드카 (로컬용)
# ───────────────────────────────────────────────────────────
try:
    from langchain_core.callbacks.base import BaseCallbackHandler

    class LLMTraceCallback(BaseCallbackHandler):
        """ChatModel 호출 1건당 로컬 사이드카 start/end + 토큰 roll-up.

        (Langfuse 추적은 별도 CallbackHandler가 담당 — 이건 로컬 trace.jsonl용.)
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
                    "content": content if isinstance(content, (str, list, dict)) else str(content),
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
                    for m in msg_list)
            except Exception:
                msg_list, prompt_chars = [], 0
            model = (serialized or {}).get("kwargs", {}).get("model", "?")
            if model == "?":
                model = (serialized or {}).get("kwargs", {}).get("model_name", "?")
            self._calls[str(run_id)] = {
                "model": model, "started_at": time.monotonic(),
                "node": _current_node.get(), "axis": _current_axis.get()}
            payload_path = payload_sha = None
            if tracer is not None:
                payload_path, payload_sha = tracer.write_payload(
                    "llm_calls", str(run_id),
                    {"phase": "start", "model": model,
                     "metadata": metadata or {}, "messages": msg_list})
            emit("llm_call_start", run_id=str(run_id), model=model,
                 prompt_chars=prompt_chars, payload_path=payload_path, payload_sha=payload_sha)

        def on_llm_end(self, response, *, run_id, parent_run_id=None, **kw):
            tracer = get_tracer()
            meta = self._calls.pop(str(run_id), None)
            duration_ms = int((time.monotonic() - meta["started_at"]) * 1000) if meta else None
            stop, usage, completion_preview, gens_out = None, {}, "", []
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
                            gens_out.append({"text": txt})
                            if not completion_preview and txt:
                                completion_preview = txt[:200]
            except Exception:
                pass
            payload_path = payload_sha = None
            if tracer is not None:
                payload_path, payload_sha = tracer.write_payload(
                    "llm_calls", str(run_id) + "_end",
                    {"phase": "end", "model": (meta or {}).get("model"),
                     "duration_ms": duration_ms, "stop_reason": stop,
                     "usage": usage, "generations": gens_out})
                tracer.add_tokens((meta or {}).get("node"), (meta or {}).get("model"), usage)
            emit("llm_call_end", run_id=str(run_id), model=(meta or {}).get("model"),
                 duration_ms=duration_ms, stop_reason=stop, usage=usage,
                 completion_preview=completion_preview,
                 payload_path=payload_path, payload_sha=payload_sha)

        def on_llm_error(self, error, *, run_id, parent_run_id=None, **kw):
            emit("error", phase="llm", run_id=str(run_id),
                 error=type(error).__name__, message=str(error)[:300])

except ImportError:
    class LLMTraceCallback:  # type: ignore
        """LangChain 미설치 fallback (no-op)."""
        pass
