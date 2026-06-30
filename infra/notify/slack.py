"""Slack webhook 에러 알림 — best-effort, non-blocking, 키 없으면 silent.

지금까지 에러는 로컬 로그(reports/{run_id}/run.log)·external.{provider} 로거에만
남아 사후에 파일을 뒤져야 보였다. 이 모듈은 ERROR+ 신호를 Slack 채널로 즉시
밀어 운영 가시성을 준다.

설계 원칙 (Langfuse silent-skip 패턴을 그대로 모방 — infra/agent/llm.py):
    - webhook URL 환경변수가 없으면 **완전 no-op** (기본 동작 무변경).
    - 알림 경로의 모든 예외를 swallow — 알림은 절대 파이프라인으로 raise/block 하지 않는다.
    - 짧은 타임아웃(~3s)으로 느린 Slack이 호출자를 잡지 않게 한다.
    - 같은 (logger, 예외타입, 메시지prefix) 알림은 N초 내 중복 억제(스팸 방지).
    - 비밀(serviceKey/api_key/token/secret …)은 텍스트에서 스크럽 — 키가 Slack에 새지 않게.

라우팅 (2채널):
    - 인증/키/쿼터/레이트리밋(429/401/403) → SLACK_WEBHOOK_API_LIMIT
    - 그 외 모든 에러                        → SLACK_WEBHOOK_ALERTS

연결 지점(enable_slack_alerting이 설치):
    (a) external.* 에러 로그        → SlackLogHandler (root 로거 attach, ERROR+)
    (b) LLM 호출 실패               → LLMTraceCallback.on_llm_error (tracing emit "error" phase=llm)
    (c) 게이트웨이 rate-limit/429   → 위 (a)/(d) 경로의 RateLimited 예외로 자연 포착
    (d) 파이프라인 노드 예외        → node_scope error 경로 (tracing emit "error" phase=node)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

logger = logging.getLogger("infra.notify.slack")

# ── 채널 식별자(논리명) → 환경변수 이름 ─────────────────────────
CHANNEL_API_LIMIT = "api_limit"   # 인증/키/쿼터/레이트리밋
CHANNEL_ALERTS = "alerts"         # 그 외 일반 에러

_ENV_BY_CHANNEL = {
    CHANNEL_API_LIMIT: "SLACK_WEBHOOK_API_LIMIT",
    CHANNEL_ALERTS: "SLACK_WEBHOOK_ALERTS",
}

# ── 라우팅 분류 신호 ────────────────────────────────────────────
# api-limit 채널로 보낼 신호: 예외 클래스명 / 메시지 / 상태코드에서 매칭.
# (클래스명이 generic한 client도 있어 — TavilyError/PublicDataPortalError —
#  message·status까지 함께 본다.)
_API_LIMIT_NAME_TOKENS = (
    "auth",            # NaverAuthError, *AuthError, Unauthorized
    "unauthor",        # Unauthorized
    "forbidden",
    "quota",           # NaverQuotaExceeded
    "ratelimit",       # *RateLimited (소문자 비교)
    "ratelimited",
    "toomanyrequest",  # TooManyRequests
    "permissiondenied",
    "credential",
)
# 메시지 본문에서 잡는 토큰 (data.go.kr resultMsg, HTTP 상태 문구 등).
_API_LIMIT_MSG_TOKENS = (
    "401", "403", "429",
    "unauthorized", "forbidden", "rate limit", "ratelimit",
    "too many requests", "quota", "auth",
    "service_key", "servicekey",            # data.go.kr 키 미등록
    "not_registered", "not registered",
    "limited_number", "limitednumber",      # data.go.kr 요청 한도
    "deadline_has_expired", "키 오류", "인증",
)
# 상태코드(int)로 잡는 경우.
_API_LIMIT_STATUS = {401, 403, 429}


def classify_channel(
    *,
    exc_type: str | None = None,
    message: str | None = None,
    status: int | None = None,
    logger_name: str | None = None,
) -> str:
    """에러 신호를 채널 논리명으로 분류한다.

    인증/키/쿼터/레이트리밋 → CHANNEL_API_LIMIT, 그 외 → CHANNEL_ALERTS.
    예외 클래스명·메시지·HTTP 상태코드를 모두 본다(클래스명만으론 부족한
    generic client 대응).
    """
    name = (exc_type or "").lower()
    if any(tok in name for tok in _API_LIMIT_NAME_TOKENS):
        return CHANNEL_API_LIMIT
    if status is not None and int(status) in _API_LIMIT_STATUS:
        return CHANNEL_API_LIMIT
    msg = (message or "").lower()
    if any(tok in msg for tok in _API_LIMIT_MSG_TOKENS):
        return CHANNEL_API_LIMIT
    return CHANNEL_ALERTS


def classify_exception(exc: BaseException, *, logger_name: str | None = None) -> str:
    """예외 객체 → 채널 논리명. status/status_code 속성이 있으면 활용."""
    status = None
    for attr in ("status", "status_code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            status = v
            break
    return classify_channel(
        exc_type=type(exc).__name__,
        message=str(exc),
        status=status,
        logger_name=logger_name,
    )


# ── 비밀 스크럽 ─────────────────────────────────────────────────
# 키 이름 토큰 — 이걸 (접두사 포함) 끝에 달고 = 또는 : 뒤에 값이 오면 비밀로 본다.
# 예: serviceKey / NAVER_CLIENT_SECRET / api_key / access_token / X-Api-Key.
_SECRET_KEY_TOKENS = (
    "servicekey", "secret", "apikey", "api_key", "access_token",
    "token", "password", "passwd", "pwd", "client_secret", "authorization",
)
# key 부분: [영숫자_\-]*(토큰). 토큰 앞에 prefix(NAVER_CLIENT_ 등)를 허용하되,
# 단어 중간 우연 매치를 줄이려 시작은 (^|비단어)로 앵커.
_KEY_NAME = r"[A-Za-z0-9_\-]*(?:" + "|".join(_SECRET_KEY_TOKENS) + r")"
# 1) "key": "VALUE"  또는  "key": VALUE  또는  key=VALUE  (따옴표 선택).
#    값이 따옴표로 감싸였으면 그 안을, 아니면 구분자 전까지를 마스킹.
_SECRET_KV_RE = re.compile(
    r'(?i)(["\']?)(' + _KEY_NAME + r')\1\s*[=:]\s*'
    r'(?:"([^"]*)"|\'([^\']*)\'|([^&\s,"\'\\}{]+))')
# 2) Bearer <token> / KakaoAK <token> (헤더형) — KV보다 먼저 돌린다.
_SECRET_BEARER_RE = re.compile(r"(?i)\b(bearer|kakaoak)\s+([A-Za-z0-9._\-]{6,})")


def _kv_sub(m: "re.Match") -> str:
    key = m.group(2)
    # 값은 따옴표 그룹(3/4) 또는 비따옴표 그룹(5) 중 하나.
    quoted = m.group(3) is not None or m.group(4) is not None
    if quoted:
        return f'"{key}": "***redacted***"'
    return f"{key}=***redacted***"


def redact(text: str) -> str:
    """텍스트에서 API 키/토큰/시크릿 값을 ***redacted***로 치환 (best-effort).

    커버: serviceKey=...·api_key:"..."·NAVER_CLIENT_SECRET=...·Bearer <tok>·
    KakaoAK <tok>·JSON {"token":"..."}. 실패 시 원문 노출 대신 통째로 가린다.
    """
    if not text:
        return text
    try:
        out = _SECRET_BEARER_RE.sub(lambda m: f"{m.group(1)} ***redacted***", text)
        out = _SECRET_KV_RE.sub(_kv_sub, out)
        return out
    except Exception:
        return "[redaction-failed]"


# ───────────────────────────────────────────────────────────────
# SlackNotifier
# ───────────────────────────────────────────────────────────────
class SlackNotifier:
    """webhook URL로 JSON 페이로드를 POST하는 best-effort 통지기.

    - URL은 채널 논리명(api_limit/alerts)별로 보관. 없으면 그 채널은 no-op.
    - dedup: (logger, exc_type, msg_prefix) 동일 알림을 throttle_seconds 내 억제.
    - 모든 예외를 삼킨다 — 절대 호출부로 raise하지 않는다.

    테스트는 transport(_post)를 주입(post_fn)해 (channel, payload)만 캡처할 수 있다.
    """

    def __init__(
        self,
        *,
        webhooks: dict[str, str] | None = None,
        timeout: float = 3.0,
        throttle_seconds: float = 60.0,
        msg_prefix_len: int = 80,
        post_fn: Callable[[str, dict], None] | None = None,
        env: dict | None = None,
    ):
        env = env if env is not None else os.environ
        if webhooks is None:
            webhooks = {}
            for channel, env_key in _ENV_BY_CHANNEL.items():
                url = (env.get(env_key) or "").strip()
                if url:
                    webhooks[channel] = url
        self._webhooks = webhooks
        self._timeout = timeout
        self._throttle_seconds = throttle_seconds
        self._msg_prefix_len = msg_prefix_len
        # post_fn(url, payload) 주입 시 실제 네트워크 대신 그것을 사용(테스트).
        self._post_fn = post_fn
        self._seen: dict[tuple, float] = {}
        self._lock = threading.Lock()

    # ── 상태 조회 ────────────────────────────────────────────
    def is_enabled(self) -> bool:
        """어느 채널이든 webhook이 설정돼 있으면 True. 둘 다 없으면 완전 no-op."""
        return bool(self._webhooks)

    def channel_url(self, channel: str) -> str | None:
        return self._webhooks.get(channel)

    # ── dedup ───────────────────────────────────────────────
    def _dedup_ok(self, key: tuple) -> bool:
        """이 (channel, logger, exc_type, prefix) 키를 지금 보낼지. throttle 내 중복은 False."""
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and (now - last) < self._throttle_seconds:
                return False
            self._seen[key] = now
            # 가벼운 GC: 오래된 항목 제거(맵 무한 성장 방지).
            if len(self._seen) > 512:
                cutoff = now - self._throttle_seconds
                self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}
            return True

    # ── 공개: 에러 1건 통지 ──────────────────────────────────
    def notify_error(
        self,
        *,
        title: str,
        exc_type: str | None = None,
        message: str | None = None,
        status: int | None = None,
        logger_name: str | None = None,
        source: str | None = None,
        level: str = "ERROR",
        extra: dict | None = None,
        force_channels: list[str] | None = None,
    ) -> bool:
        """분류→dedup→POST. 보냈으면(1채널 이상) True, (no-op/throttle/실패) False.

        force_channels를 주면 분류를 건너뛰고 그 채널들에 모두 보낸다(외부 오류를
        api_limit·alerts 동시 통지 등). 절대 raise하지 않는다.
        """
        try:
            if force_channels is not None:
                channels = list(force_channels)
            else:
                channels = [classify_channel(
                    exc_type=exc_type, message=message, status=status,
                    logger_name=logger_name)]

            safe_msg = redact(message or "")
            prefix = safe_msg[: self._msg_prefix_len]
            sent_any = False
            for channel in channels:
                url = self._webhooks.get(channel)
                if not url:
                    continue  # 채널 미설정 → silent no-op
                dedup_key = (channel, logger_name or source or "", exc_type or "", prefix)
                if not self._dedup_ok(dedup_key):
                    continue
                payload = self._build_payload(
                    channel=channel, title=title, exc_type=exc_type,
                    message=safe_msg, status=status, logger_name=logger_name,
                    source=source, level=level, extra=extra)
                self._dispatch(channel, url, payload)
                sent_any = True
            return sent_any
        except Exception as e:  # 알림은 절대 호출부를 깨지 않는다
            logger.debug("slack notify_error swallowed: %s", e)
            return False

    def notify_exception(
        self,
        exc: BaseException,
        *,
        title: str | None = None,
        logger_name: str | None = None,
        source: str | None = None,
        level: str = "ERROR",
        extra: dict | None = None,
    ) -> bool:
        """예외 객체 편의 래퍼."""
        status = None
        for attr in ("status", "status_code"):
            v = getattr(exc, attr, None)
            if isinstance(v, int):
                status = v
                break
        exc_type = type(exc).__name__
        return self.notify_error(
            title=title or f"{exc_type}",
            exc_type=exc_type, message=str(exc), status=status,
            logger_name=logger_name, source=source, level=level, extra=extra)

    # ── 페이로드 + 전송 ─────────────────────────────────────
    def _build_payload(self, *, channel, title, exc_type, message, status,
                       logger_name, source, level, extra) -> dict:
        emoji = {"api_limit": ":lock:"}.get(channel, ":rotating_light:")
        header = f"{emoji} [{level}] {redact(str(title))}"
        lines = []
        if source:
            lines.append(f"*source*: `{source}`")
        if logger_name:
            lines.append(f"*logger*: `{logger_name}`")
        if exc_type:
            lines.append(f"*type*: `{exc_type}`")
        if status is not None:
            lines.append(f"*status*: `{status}`")
        if message:
            # Slack 코드블록 — 비밀은 이미 redact됨, 길이 컷.
            lines.append("```" + message[:1500] + "```")
        if extra:
            try:
                for k, v in extra.items():
                    lines.append(f"*{k}*: `{redact(str(v))[:200]}`")
            except Exception:
                pass
        return {"text": header + "\n" + "\n".join(lines)}

    def _dispatch(self, channel: str, url: str, payload: dict) -> None:
        if self._post_fn is not None:
            self._post_fn(channel, payload)   # 테스트 캡처 경로
            return
        self._post(url, payload)

    def _post(self, url: str, payload: dict) -> None:
        """stdlib urllib로 webhook POST. 짧은 타임아웃, 실패는 삼킨다."""
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            logger.debug("slack POST failed (swallowed): %s", e)
        except Exception as e:
            logger.debug("slack POST error (swallowed): %s", e)


# ───────────────────────────────────────────────────────────────
# 프로세스 전역 통지기 + 활성화
# ───────────────────────────────────────────────────────────────
_notifier: SlackNotifier | None = None
_notifier_lock = threading.Lock()
_log_handler: "SlackLogHandler | None" = None
_tracing_hooked = False


def get_notifier() -> SlackNotifier | None:
    """현재 활성화된 전역 SlackNotifier (없으면 None)."""
    return _notifier


def set_notifier(notifier: SlackNotifier | None) -> None:
    """전역 통지기 교체 (테스트에서 mock 주입용)."""
    global _notifier
    with _notifier_lock:
        _notifier = notifier


class SlackLogHandler(logging.Handler):
    """ERROR+ 로그 레코드를 SlackNotifier로 전달하는 logging 핸들러.

    root 로거에 붙이면 external.* / infra.* / graphflow.* 의 ERROR가 모두 잡힌다.
    `emit`은 절대 raise하지 않는다(logging.Handler.handleError로도 감싸지만,
    여기서 한 번 더 swallow).
    """

    def __init__(self, notifier: SlackNotifier, *, level: int = logging.ERROR):
        super().__init__(level=level)
        self._notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < self.level:
                return
            # 자기 자신/알림 모듈 로그는 무시(피드백 루프 방지).
            if record.name.startswith("infra.notify"):
                return
            exc_type = None
            status = None
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                exc_type = type(exc).__name__
                for attr in ("status", "status_code"):
                    v = getattr(exc, attr, None)
                    if isinstance(v, int):
                        status = v
                        break
                # 메시지에 예외 문구도 합쳐 분류 정확도 ↑
                msg = f"{msg} | {exc}"
            self._notifier.notify_error(
                title=record.name,
                exc_type=exc_type,
                message=msg,
                status=status,
                logger_name=record.name,
                source=f"{record.module}:{record.lineno}",
                level=record.levelname,
            )
        except Exception:
            # logging 핸들러는 절대 raise하면 안 됨.
            try:
                self.handleError(record)
            except Exception:
                pass


def enable_slack_alerting(
    *,
    notifier: SlackNotifier | None = None,
    attach_root: bool = True,
    hook_tracing: bool = True,
    level: int = logging.ERROR,
) -> SlackNotifier | None:
    """Slack 알림 설치 — 4개 연결 지점을 배선한다. webhook 없으면 silent no-op.

    멱등: 이미 설치돼 있으면 기존 통지기를 반환(중복 attach 안 함).

    배선:
        (a) external.* 등 ERROR+ 로그 → SlackLogHandler를 root 로거에 attach.
        (b)(d) tracing의 emit("error", phase=llm|node) 경로 → notify (게이트웨이
            429/auth 예외는 (a)의 로그 또는 (d)의 노드 에러로 자연 포착).

    Returns: 활성 SlackNotifier 또는 None(미설정).
    """
    global _notifier, _log_handler, _tracing_hooked

    with _notifier_lock:
        if notifier is None:
            notifier = SlackNotifier()
        # webhook이 하나도 없으면 아무것도 설치하지 않는다 (기본 동작 무변경).
        if not notifier.is_enabled():
            logger.info("Slack webhook 미설정 — 알림 비활성 (no-op)")
            _notifier = notifier  # get_notifier()가 일관되게 동작하도록 보관
            return None

        _notifier = notifier

        if attach_root and _log_handler is None:
            handler = SlackLogHandler(notifier, level=level)
            logging.getLogger().addHandler(handler)
            _log_handler = handler
            logger.info("Slack 알림 활성 — root 로거 ERROR+ → Slack")

        if hook_tracing and not _tracing_hooked:
            _install_tracing_hook(notifier)
            _tracing_hooked = True

    return notifier


def disable_slack_alerting() -> None:
    """설치한 핸들러/훅 제거 (테스트 정리용). tracing 훅은 emit 재설치로 복원."""
    global _notifier, _log_handler, _tracing_hooked
    with _notifier_lock:
        if _log_handler is not None:
            try:
                logging.getLogger().removeHandler(_log_handler)
            except Exception:
                pass
            _log_handler = None
        _notifier = None
        # _tracing_hooked는 유지 — wrapper가 get_notifier()를 보므로
        # notifier=None이면 자동 no-op (중복 wrap 방지 위해 재설치 안 함).


# ── (b)(d) tracing emit("error") 후킹 ───────────────────────────
def _install_tracing_hook(notifier: SlackNotifier) -> None:
    """infra.agent.tracing.emit를 감싸 phase∈{llm,node}의 error를 Slack로 전달.

    emit는 LLMTraceCallback.on_llm_error / node_scope의 error 경로가 호출한다.
    tracing은 langfuse/external을 모르지만 emit 한 곳만 가로채면 (b)(d) 둘 다 잡힌다.
    wrapper는 get_notifier()를 보므로 disable 후엔 자동 no-op이 된다.
    """
    try:
        import infra.agent.tracing as tracing
    except Exception as e:
        logger.debug("tracing hook skip (import): %s", e)
        return

    if getattr(tracing, "_slack_emit_wrapped", False):
        return
    original_emit = tracing.emit

    def emit_with_slack(event_type: str, **fields):
        try:
            original_emit(event_type, **fields)
        finally:
            if event_type == "error":
                _maybe_notify_from_emit(fields)

    tracing.emit = emit_with_slack            # type: ignore[assignment]
    tracing._slack_emit_wrapped = True         # type: ignore[attr-defined]


def _maybe_notify_from_emit(fields: dict) -> None:
    """tracing emit('error', ...) 필드 → Slack notify. 절대 raise 안 함."""
    try:
        n = get_notifier()
        if n is None:
            return
        phase = fields.get("phase")  # 'llm' | 'node' | ...
        if phase not in ("llm", "node"):
            return
        exc_type = fields.get("error")
        message = fields.get("message") or ""
        node = fields.get("node")
        run_id = fields.get("run_id")
        extra = {k: v for k, v in (("node", node), ("run_id", run_id)) if v}
        n.notify_error(
            title=f"pipeline {phase} error" + (f" @ {node}" if node else ""),
            exc_type=exc_type,
            message=message,
            logger_name=f"graphflow.{phase}",
            source=f"tracing.emit:{phase}",
            level="ERROR",
            extra=extra or None,
        )
    except Exception as e:
        logger.debug("slack emit-hook swallowed: %s", e)
