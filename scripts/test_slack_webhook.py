"""Slack 웹훅 실전송 테스트 — 에이전트의 외부 API 호출 경로를 그대로 타서, 웹훅이
실제로 작동하는지 검증한다. **LLM 무개입.**

배경(왜 이 경로인가):
    에이전트는 외부 API를 `infra/external/_http.py`의 transport
    (`timed_urlopen`/`timed_requests_get`)로 호출한다. 그 transport는 응답이
    4xx/5xx거나 네트워크 오류면 `_notify_api_error(api_name, status, body)` →
    `get_notifier().notify_error(...)` → Slack POST 한다(LLM과 무관).
    이 스크립트는 **바로 그 transport**를, 차단 위험이 없는 무해한 상태코드 echo
    API(httpstat.us)에 **잘못된 키**로 반복 호출해 동일한 알림 경로를 재현한다. 즉
    "에이전트가 외부 API 실패 시 쏘는 알림과 동일한 양식"이다.

검증 방식:
    전역 SlackNotifier를 설치(enable_slack_alerting)하되, 네트워크 POST를 감싼
    `post_fn`을 주입해 **실제 Slack 응답 HTTP 코드를 캡처**한다. 200이면 webhook 정상.
    (참고: music_copyright 파이프라인은 enable_slack_alerting을 호출하지 않으므로
     평소엔 이 알림이 비활성이다 — real_estate workflow만 설치. 이 스크립트가 그 설치를
     대신 수행해 음악 경로에서도 동일 transport 알림을 시험한다.)

라우팅(분류는 notifier가 status/메시지로):
    401·403·429  → api_limit 채널(SLACK_WEBHOOK_API_LIMIT)
    그 외(500 등) → alerts   채널(SLACK_WEBHOOK_ALERTS)

사용:
    python -m scripts.test_slack_webhook                    # 401/403/429/500 사이클 4회 (httpstat.us)
    python -m scripts.test_slack_webhook --count 8 --interval 1
    python -m scripts.test_slack_webhook --connectivity-only  # 채널별 직접 POST 1회씩만
    python -m scripts.test_slack_webhook --dry-run            # 실제 전송 없이 라우팅/분류만
    python -m scripts.test_slack_webhook --target tavily      # 실 provider client(BAD key)로 동일 경로

주의: --dry-run이 아니면 **실제 Slack 채널에 `[웹훅 테스트]` 메시지가 전송된다.**
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

from infra.env import load_env
from infra.notify.slack import (
    CHANNEL_ALERTS,
    CHANNEL_API_LIMIT,
    SlackNotifier,
    enable_slack_alerting,
)

# 채널 → webhook URL을 담은 env 변수 (slack.py _ENV_BY_CHANNEL와 동일).
_CHANNEL_ENV = {
    CHANNEL_API_LIMIT: "SLACK_WEBHOOK_API_LIMIT",
    CHANNEL_ALERTS: "SLACK_WEBHOOK_ALERTS",
}

# 401/403/429 → api_limit, 500 → alerts. 사이클로 양 채널을 모두 시험.
_STATUS_CYCLE = [401, 403, 429, 500]

_STATUS_ECHO = "https://httpstat.us"   # 요청한 상태코드를 그대로(+본문에 코드 텍스트) 돌려주는 무해한 테스트 API


def _make_post_fn(records: list, *, dry: bool):
    """notifier가 호출하는 post_fn(channel, payload). 실제 Slack에 POST하고 HTTP
    응답코드를 records에 캡처한다(검증용). dry면 전송 없이 분류만 기록."""

    def post_fn(channel: str, payload: dict) -> None:
        if dry:
            records.append({"channel": channel, "http": None, "note": "dry-run(미전송)"})
            return
        url = (os.environ.get(_CHANNEL_ENV[channel]) or "").strip()
        if not url:
            records.append({"channel": channel, "http": None, "note": "webhook URL 없음"})
            return
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                body = resp.read().decode("utf-8", "replace")
                records.append({"channel": channel, "http": resp.getcode(), "note": body[:40]})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:80]
            records.append({"channel": channel, "http": exc.code, "note": detail})
        except Exception as exc:  # noqa: BLE001
            records.append({"channel": channel, "http": None, "note": f"{type(exc).__name__}: {exc}"[:80]})

    return post_fn


def _exercise_status_echo(count: int, interval: float) -> None:
    """에이전트와 동일한 transport(_http.timed_urlopen)로 무해한 상태코드 echo API를
    '잘못된 키'로 반복 호출 → 4xx/5xx → _notify_api_error → notifier → Slack POST.

    httpstat.us는 본문에 코드 텍스트("401 Unauthorized" 등)를 담아 주므로 채널 분류가
    결정적이고(401/403/429→api_limit, 500→alerts), 코드마다 본문이 달라 dedup도 자연 회피."""
    from infra.external import _http
    from infra.gateway.gateway import get_default

    gateway = get_default()
    for i in range(count):
        code = _STATUS_CYCLE[i % len(_STATUS_CYCLE)]
        # '잘못된 키'를 쿼리·헤더에 실어 에이전트의 keyed 호출과 동일한 모양으로 만든다.
        # (echo API는 키를 무시하지만 요청 shape는 동일)
        bad_key = f"WRONG-KEY-{i}-{code}"
        url = f"{_STATUS_ECHO}/{code}?api_key={bad_key}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {bad_key}", "Accept": "text/plain"})
        try:
            _http.timed_urlopen(
                req, api_name="webhook_test", gateway=gateway, timeout=5.0,
                on_http_error=lambda s, b: RuntimeError(f"HTTP {s}"),
                on_url_error=lambda r: RuntimeError(f"URL error: {r}"))
        except Exception:  # noqa: BLE001
            # transport가 이미 _notify_api_error로 알림을 쐈다. 더미 client 예외는 무시.
            pass
        print(f"  [{i + 1}/{count}] status={code} bad_key={bad_key} → 알림 트리거")
        if interval > 0:
            time.sleep(interval)


def _exercise_tavily(count: int, interval: float) -> None:
    """실 provider client(Tavily)를 잘못된 키로 호출 → 동일 _http transport 경유.
    실제 provider 401을 발생시키는 가장 '에이전트에 가까운' 모드."""
    from infra.external.tavily.client import TavilyClient

    for i in range(count):
        bad_key = f"tvly-INVALIDKEY-{i}"
        try:
            TavilyClient(api_key=bad_key).search("webhook test", max_results=1)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i + 1}/{count}] tavily BAD-key={bad_key} → {type(exc).__name__} (알림 트리거)")
        if interval > 0:
            time.sleep(interval)


def _connectivity_check(notifier: SlackNotifier) -> None:
    """채널별로 직접 notify_error 1회씩 — webhook POST 자체의 연결만 확인(transport 미경유)."""
    notifier.notify_error(title="[웹훅 테스트] 연결 확인 (api_limit)",
                          message="connectivity check — 401 unauthorized",
                          status=401, logger_name="webhook_test", source="webhook_test")
    notifier.notify_error(title="[웹훅 테스트] 연결 확인 (alerts)",
                          message="connectivity check — 500 internal error",
                          status=500, logger_name="webhook_test", source="webhook_test")


def main() -> int:
    p = argparse.ArgumentParser(description="Slack 웹훅 실전송 테스트 (LLM 무개입)")
    p.add_argument("--count", type=int, default=4, help="반복 호출 횟수 (기본 4 = 상태코드 1사이클)")
    p.add_argument("--interval", type=float, default=0.5, help="호출 간 대기(초)")
    p.add_argument("--target", choices=["status", "tavily"], default="status",
                   help="status(무해한 httpstat.us 상태코드 echo, 기본) / tavily(실 provider, BAD key)")
    p.add_argument("--throttle", type=float, default=0.0,
                   help="notifier dedup 창(초). 기본 0=중복 억제 끄고 매 호출 전송. "
                        "프로덕션 기본은 60초(동일 알림 1회).")
    p.add_argument("--connectivity-only", action="store_true",
                   help="transport 미경유, 채널별 직접 POST 1회씩만")
    p.add_argument("--dry-run", action="store_true", help="실제 전송 없이 라우팅/분류만")
    args = p.parse_args()

    load_env()

    # 설정된 채널 확인
    configured = {ch: bool((os.environ.get(env) or "").strip())
                  for ch, env in _CHANNEL_ENV.items()}
    print("=== Slack webhook 설정 상태 ===")
    for ch, ok in configured.items():
        print(f"  {ch:10s} ({_CHANNEL_ENV[ch]}): {'설정됨' if ok else '없음'}")
    if not any(configured.values()):
        print("\n❌ SLACK_WEBHOOK_API_LIMIT / SLACK_WEBHOOK_ALERTS 둘 다 없음 — 테스트 불가.")
        return 2

    # notifier 설치 — post_fn 주입으로 실제 POST + HTTP 응답코드 캡처.
    records: list[dict] = []
    notifier = SlackNotifier(post_fn=_make_post_fn(records, dry=args.dry_run),
                             throttle_seconds=args.throttle)
    enable_slack_alerting(notifier=notifier, attach_root=False, hook_tracing=False)
    mode = "DRY-RUN(미전송)" if args.dry_run else "실전송"
    print(f"\n=== 알림 발생 ({mode}, target={args.target}) ===")

    if args.connectivity_only:
        _connectivity_check(notifier)
    elif args.target == "tavily":
        _exercise_tavily(args.count, args.interval)
    else:
        _exercise_status_echo(args.count, args.interval)

    # 결과 집계
    print("\n=== 결과 (Slack POST 응답) ===")
    if not records:
        print("  (전송된 알림 없음 — dedup/미설정 채널이거나 모든 호출이 성공했을 수 있음)")
    by_status: dict = {}
    for r in records:
        key = (r["channel"], r["http"])
        by_status[key] = by_status.get(key, 0) + 1
        print(f"  {r['channel']:10s} HTTP={str(r['http']):5s}  {r['note']}")
    ok_200 = [r for r in records if r["http"] == 200]
    print("\n=== 요약 ===")
    print(f"  발생 알림: {len(records)}건")
    if args.dry_run:
        chans = sorted({r['channel'] for r in records})
        print(f"  (dry-run) 라우팅 채널: {chans or '없음'} — 분류 정상")
    elif ok_200:
        print(f"  ✅ webhook 정상 — Slack 200 OK {len(ok_200)}건 (채널: "
              f"{sorted({r['channel'] for r in ok_200})})")
    elif records:
        print("  ⚠️ 전송 시도됐으나 200 없음 — webhook URL 무효/만료 가능. 위 응답코드 확인.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
