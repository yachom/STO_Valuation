"""외부 API 호출의 통합 기록·알림 (단일 경유점).

모든 외부 HTTP 응답을 **로컬** `run_dir/responses.jsonl`에 남기고(추후 에러코드 분석용
영구 보관), **오류만** Slack 두 채널로 통지한다. 왜 이렇게 나누나:

    Slack 인입 웹훅은 ~1 msg/초 제한이라, 한 번의 평가 실행(수백 외부호출)의 '모든 응답'을
    Slack에 보내면 429로 대부분 드랍되고 파이프라인도 느려진다. 그래서 전수 기록은 로컬
    JSONL(무제한·질의 가능)에, Slack은 '지금 뭔가 깨졌다' 신호(오류)만 보낸다.

오류 정의(잠정): 2xx 이외의 모든 응답 + 예외/네트워크 실패. (서비스별 에러코드 세분화는
추후 과제 — 지금은 비-2xx를 일괄 오류로 본다.) 오류는 두 채널에 동시 통지한다:
    - api_limit : 모든 외부 오류를 모으는 채널(이름과 달리 전 오류 로그용으로 사용).
    - alerts    : 가시성 채널(오류 + 실행종료 요약).

메시지는 '메시지만 보고 어디서 무엇이 문제인지' 알 수 있게 자산/단계/호출 컨텍스트를 박는다:
    asset_type:asset_id · phase · node · axis · run_id · api_name.operation · endpoint ·
    status · response/error 발췌 · elapsed_ms.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from typing import Any

from infra.notify.slack import (
    CHANNEL_ALERTS,
    CHANNEL_API_LIMIT,
    get_notifier,
    redact,
)

logger = logging.getLogger("infra.external.audit")

_RESP_FILE = "responses.jsonl"
_write_lock = threading.Lock()
_RESP_EXCERPT = 500

# 자산/단계 컨텍스트 — 파이프라인 진입부가 set_external_context로 세팅하면 이후 모든
# 외부호출 기록·알림에 자동 부착된다(깊은 transport까지 인자 전파 없이 contextvar로).
_external_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "external_ctx", default={})


def set_external_context(*, asset_type: str | None = None,
                         asset_id: str | None = None,
                         phase: str | None = None) -> None:
    """현재 컨텍스트(asset/단계)를 누적 세팅. 준 값만 갱신."""
    cur = dict(_external_ctx.get() or {})
    if asset_type is not None:
        cur["asset_type"] = asset_type
    if asset_id is not None:
        cur["asset_id"] = asset_id
    if phase is not None:
        cur["phase"] = phase
    _external_ctx.set(cur)


def clear_external_context() -> None:
    _external_ctx.set({})


def _ctx() -> dict:
    """자산 컨텍스트 + tracing(run_id/node/axis)을 합친 메시지 컨텍스트."""
    ctx = dict(_external_ctx.get() or {})
    try:
        from infra.agent.tracing import current_axis, current_node, get_tracer
        t = get_tracer()
        if t is not None:
            ctx.setdefault("run_id", t.run_id)
        node = current_node()
        axis = current_axis()
        if node:
            ctx.setdefault("node", node)
        if axis:
            ctx.setdefault("axis", axis)
    except Exception:  # noqa: BLE001 — tracing 미설치여도 기록은 계속
        pass
    return ctx


def _run_dir():
    try:
        from infra.agent.tracing import get_tracer
        t = get_tracer()
        return t.run_dir if t is not None else None
    except Exception:  # noqa: BLE001
        return None


def is_error(status: int | None, error: Any = None) -> bool:
    """오류 판정: 예외 있음 · status 없음(네트워크 실패) · 2xx 아님 → 오류."""
    if error is not None:
        return True
    if status is None:
        return True
    try:
        return not (200 <= int(status) < 300)
    except (TypeError, ValueError):
        return True


def _trim(text: Any) -> str | None:
    if text is None:
        return None
    s = text if isinstance(text, str) else str(text)
    return s[:_RESP_EXCERPT]


def _safe(text: Any) -> str | None:
    """redact + 절단. 비밀(키/토큰)이 로그/Slack에 새지 않게."""
    t = _trim(text)
    return redact(t) if t else t


def record_response(
    *,
    api_name: str,
    operation: str | None = None,
    status: int | None = None,
    endpoint: str | None = None,
    request_summary: Any = None,
    response_excerpt: Any = None,
    error: Any = None,
    elapsed_ms: int | None = None,
) -> None:
    """외부 호출 1건 기록. 모든 응답→로컬 jsonl, 오류만→Slack 두 채널. 절대 raise 안 함."""
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **_ctx(),
            "api_name": api_name,
            "operation": operation,
            "endpoint": _safe(endpoint),
            "status": status,
            "request": _safe(request_summary),
            "response": _safe(response_excerpt),
            "error": _safe(error) if error is not None else None,
            "elapsed_ms": elapsed_ms,
        }
        _append_jsonl(rec)
        if is_error(status, error):
            _notify(rec)
    except Exception as exc:  # noqa: BLE001 — 기록/알림은 절대 호출부를 깨지 않는다
        logger.debug("record_response swallowed: %s", exc)


def _append_jsonl(rec: dict) -> None:
    rd = _run_dir()
    if rd is None:
        return  # 트레이서 없으면(단발 스크립트 등) 로컬 기록 생략
    try:
        line = json.dumps(rec, ensure_ascii=False)
        with _write_lock:
            with open(rd / _RESP_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("responses.jsonl write skip: %s", exc)


def _notify(rec: dict) -> None:
    """오류 1건 → Slack api_limit + alerts 동시. 메시지만 보고 위치 파악 가능하게."""
    n = get_notifier()
    if n is None:
        return
    loc = f"{rec.get('asset_type', '?')}:{rec.get('asset_id', '?')}"
    op = rec.get("operation") or ""
    title = f"외부 API 오류: {rec['api_name']}{('.' + op) if op else ''} [{loc}]"
    body = rec.get("response") or rec.get("error") or ""
    extra = {k: rec[k] for k in (
        "asset_type", "asset_id", "phase", "node", "axis", "run_id",
        "operation", "endpoint", "elapsed_ms") if rec.get(k) is not None}
    n.notify_error(
        title=title, message=str(body), status=rec.get("status"),
        logger_name=f"external.{rec['api_name']}", source=rec["api_name"],
        extra=extra, force_channels=[CHANNEL_API_LIMIT, CHANNEL_ALERTS])


def flush_summary() -> None:
    """실행 종료 시 responses.jsonl을 읽어 alerts 채널에 요약 1건(총/오류/서비스별)."""
    rd = _run_dir()
    if rd is None:
        return
    path = rd / _RESP_FILE
    if not path.exists():
        return
    total = 0
    errors = 0
    by_api: dict[str, list[int]] = {}   # api → [ok, err]
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                total += 1
                err = is_error(r.get("status"), r.get("error"))
                errors += int(err)
                slot = by_api.setdefault(r.get("api_name") or "?", [0, 0])
                slot[1 if err else 0] += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("flush_summary read skip: %s", exc)
        return
    if total == 0:
        return
    n = get_notifier()
    if n is None:
        return
    ctx = _ctx()
    loc = f"{ctx.get('asset_type', '?')}:{ctx.get('asset_id', '?')}"
    breakdown = ", ".join(
        f"{api}={ok + er}({er}err)" for api, (ok, er) in sorted(by_api.items()))
    n.notify_error(
        title=f"외부 호출 요약 [{loc}]",
        message=f"총 {total}건 · 오류 {errors}건\n서비스별: {breakdown}",
        logger_name="external.summary", source="external_summary",
        level="INFO", extra={"run_id": ctx.get("run_id")},
        force_channels=[CHANNEL_ALERTS])
