"""보고서 생성 1회 run의 상세 trace 수집.

3종 산출물 (보고서 디렉토리 = real_estate/database/DB/reports/{report_id}/):
    run.log     — human-readable, 모든 logger output (Python logging FileHandler)
    trace.jsonl — machine-readable 이벤트 trace (한 줄 = JSON 한 객체)
    {version_id}.md — 보고서 본문 (기존)

이벤트 종류 (event_type):
    pipeline_start / pipeline_end
    retriever_init
    node_start / node_end           ← graph_v2의 각 노드 진입/종료
    llm_call_start / llm_call_end  ← LangChain BaseCallbackHandler
    scout_call                      ← DataScout 함수 호출 1건당 1 event
    error                           ← Exception fallback 처리 시

전역 패턴:
    `get_tracer()` 가 contextvars 기반 현재 RunTracer 반환. 노드/scout는 이걸
    가져와 emit. 파이프라인 시작 시 RunTracer를 한 번만 만들고 set.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger("tracing")

_current_tracer: contextvars.ContextVar["RunTracer | None"] = contextvars.ContextVar(
    "run_tracer", default=None,
)


def make_report_id(prefix: str = "rpt") -> str:
    """타임스탬프 prefix가 박힌 report_id.

    예: rpt_20260604_113042_8a4f7c1d (정렬 가능 + 가독성).
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid4().hex[:8]}"


class RunTracer:
    """1회 보고서 생성 run의 trace 수집기. thread-safe (단일 프로세스 가정)."""

    def __init__(self, report_id: str, db_path: Path):
        self._report_id = report_id
        self._run_dir = db_path.parent / "reports" / report_id
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self._run_dir / "trace.jsonl"
        self._log_path = self._run_dir / "run.log"
        self._t0 = time.monotonic()
        self._jsonl_fh = open(self._jsonl_path, "a", encoding="utf-8")
        self._file_handler = self._install_file_handler()
        self.emit("pipeline_start", report_id=report_id)

    @property
    def report_id(self) -> str:
        return self._report_id

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def emit(self, event_type: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "elapsed_ms": int((time.monotonic() - self._t0) * 1000),
            "event": event_type,
            **fields,
        }
        self._jsonl_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._jsonl_fh.flush()
        logger.info("trace [%s] %s", event_type, _short(fields))

    def _install_file_handler(self) -> logging.Handler:
        """모든 Python logger의 output을 run.log에도 기록."""
        fh = logging.FileHandler(self._log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        ))
        logging.getLogger().addHandler(fh)
        return fh

    def close(self) -> None:
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
    """logger 출력용 한 줄 요약 (긴 prompt/output은 생략)."""
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
    """현재 tracer가 있으면 emit, 없으면 silent. 노드/scout 측 호출 단순화."""
    t = _current_tracer.get()
    if t is not None:
        t.emit(event_type, **fields)


# ───────────────────────────────────────────────────────────
# LangChain Callback — LLM 호출 자동 trace
# ───────────────────────────────────────────────────────────
try:
    from langchain_core.callbacks.base import BaseCallbackHandler

    class LLMTraceCallback(BaseCallbackHandler):
        """ChatAnthropic 호출 1건당 start/end 이벤트 기록.

        on_chat_model_start: prompt 메시지 / model 이름
        on_llm_end: output usage (tokens), stop_reason
        on_llm_error: error
        """
        def __init__(self):
            self._calls: dict[str, dict] = {}

        def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kw):
            # messages: list[list[BaseMessage]]
            try:
                flat = messages[0] if messages else []
                prompt_chars = sum(
                    len(str(m.content if hasattr(m, "content") else m))
                    for m in flat
                )
            except Exception:
                prompt_chars = 0
            model = (serialized or {}).get("kwargs", {}).get("model", "?")
            self._calls[str(run_id)] = {
                "model": model,
                "started_at": time.monotonic(),
                "prompt_chars": prompt_chars,
            }
            emit("llm_call_start", run_id=str(run_id), model=model,
                 prompt_chars=prompt_chars)

        def on_llm_end(self, response, *, run_id, parent_run_id=None, **kw):
            meta = self._calls.pop(str(run_id), None)
            duration_ms = int((time.monotonic() - meta["started_at"]) * 1000) if meta else None
            usage = {}
            try:
                gens = response.generations
                if gens and gens[0]:
                    info = gens[0][0].generation_info or {}
                    usage = response.llm_output or {}
                    stop = info.get("stop_reason") or info.get("finish_reason")
            except Exception:
                stop = None
            emit("llm_call_end",
                 run_id=str(run_id),
                 model=(meta or {}).get("model"),
                 duration_ms=duration_ms,
                 stop_reason=stop,
                 usage_tokens=usage.get("usage") or usage.get("token_usage"))

        def on_llm_error(self, error, *, run_id, parent_run_id=None, **kw):
            emit("error", phase="llm", run_id=str(run_id),
                 error=type(error).__name__, message=str(error)[:300])

except ImportError:
    class LLMTraceCallback:  # type: ignore
        """LangChain 미설치 환경 fallback (no-op)."""
        pass
