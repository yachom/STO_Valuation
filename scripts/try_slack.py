"""Slack 에러 알림 시험 — 라우팅 분류·no-op·스크럽 + 실 client 예외 shape 관찰.

실제 Slack 전송에는 진짜 webhook URL이 필요하므로(이 워크트리엔 없음), 테스트는
**mock notifier**(post_fn으로 (channel, payload)만 캡처)로 라우팅이 맞는지 검증한다.

데모하는 것:
    1. mock-capture 라우팅: 여러 에러 종류 → 올바른 채널.
    2. webhook URL 없을 때 완전 no-op (is_enabled=False, notify_error→False).
    3. 비밀 스크럽 (serviceKey/Bearer/api_key 마스킹).
    4. (--live) 실 external client를 BAD/GOOD 인자로 호출해 진짜 예외/로그 shape 관찰
       + 그 예외를 classify_exception에 통과시켜 라우팅 결정 확인.

사용:
    python -m scripts.try_slack             # 단위(라우팅/노옵/스크럽) — 키 불필요
    python -m scripts.try_slack --live      # + 실 client 예외 shape (load_env로 키 로드)
"""

from __future__ import annotations

import logging
import sys

from infra.notify.slack import (
    CHANNEL_ALERTS,
    CHANNEL_API_LIMIT,
    SlackNotifier,
    classify_channel,
    classify_exception,
    redact,
)


# ───────────────────────────────────────────────────────────────
# mock notifier — 실제 POST 대신 (channel, payload) 캡처
# ───────────────────────────────────────────────────────────────
def make_capture_notifier(**kw) -> tuple[SlackNotifier, list]:
    """두 채널 모두 webhook이 설정된 것으로 보이게 하고, POST는 캡처 리스트로 가로챈다."""
    captured: list[tuple[str, dict]] = []
    notifier = SlackNotifier(
        webhooks={CHANNEL_API_LIMIT: "https://hooks.slack/x",
                  CHANNEL_ALERTS: "https://hooks.slack/y"},
        post_fn=lambda ch, payload: captured.append((ch, payload)),
        throttle_seconds=kw.get("throttle_seconds", 0.0),
    )
    return notifier, captured


def test_routing() -> bool:
    """대표 에러 종류 → 채널 라우팅. mock 캡처로 검증."""
    print("\n=== 1) 라우팅 (mock 캡처) ===")
    # (title, exc_type, message, status, expected_channel)
    cases = [
        ("naver auth",   "NaverAuthError",      "필수 환경변수 누락: NAVER_CLIENT_ID", None, CHANNEL_API_LIMIT),
        ("naver quota",  "NaverQuotaExceeded",  "Quota exceeded (errorCode=010)",      None, CHANNEL_API_LIMIT),
        ("naver rate",   "NaverRateLimited",    "rate limited",                        429,  CHANNEL_API_LIMIT),
        ("kakao 401",    "KakaoLocalError",     "Kakao Local 호출 실패 (HTTP 401)",    401,  CHANNEL_API_LIMIT),
        ("tavily 403",   "TavilyError",         "Tavily HTTP 403: forbidden",          403,  CHANNEL_API_LIMIT),
        ("pdp key",      "PublicDataPortalError","공공데이터포털 API 오류: 30 SERVICE_KEY_IS_NOT_REGISTERED_ERROR", None, CHANNEL_API_LIMIT),
        ("pdp limit",    "PublicDataPortalError","공공데이터포털 API 오류: 22 LIMITED_NUMBER_OF_SERVICE_REQUESTS", None, CHANNEL_API_LIMIT),
        ("ddgs rate",    "DdgsRateLimited",     "레이트리밋 초과",                      None, CHANNEL_API_LIMIT),
        # ── 일반 에러 → alerts ──
        ("naver 500",    "NaverAPIError",       "server error",                        500,  CHANNEL_ALERTS),
        ("json parse",   "TavilyError",         "JSON 파싱 실패: <html>",              None, CHANNEL_ALERTS),
        ("network",      "PublicDataPortalError","공공데이터포털 호출 실패: timeout",   None, CHANNEL_ALERTS),
        ("llm error",    "APIConnectionError",  "Connection error to anthropic",       None, CHANNEL_ALERTS),
        ("node error",   "KeyError",            "'report'",                            None, CHANNEL_ALERTS),
        ("ddgs timeout", "DdgsTimeout",         "타임아웃",                             None, CHANNEL_ALERTS),
    ]
    ok = True
    for title, exc_type, msg, status, expected in cases:
        # (a) 순수 분류 함수
        got = classify_channel(exc_type=exc_type, message=msg, status=status)
        mark = "OK " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  [{mark}] {title:14s} {exc_type:24s} status={str(status):4s} → {got}")

    # (b) mock notifier로 실제 디스패치까지 — 캡처된 채널이 분류와 일치하는지
    print("\n  -- mock notifier 디스패치 캡처 --")
    notifier, captured = make_capture_notifier()
    for title, exc_type, msg, status, expected in cases:
        notifier.notify_error(title=title, exc_type=exc_type, message=msg,
                              status=status, logger_name="external.test")
    if len(captured) != len(cases):
        print(f"  [FAIL] 캡처 수 {len(captured)} != 케이스 수 {len(cases)}")
        ok = False
    else:
        for (title, exc_type, msg, status, expected), (ch, _payload) in zip(cases, captured):
            if ch != expected:
                print(f"  [FAIL] {title}: 캡처 채널 {ch} != {expected}")
                ok = False
        print(f"  캡처 {len(captured)}건 — 채널 일치 {'OK' if ok else 'FAIL'}")
    return ok


def test_noop_when_absent() -> bool:
    """webhook URL이 없으면 완전 no-op — is_enabled False, notify_error False."""
    print("\n=== 2) webhook 없음 → no-op ===")
    notifier = SlackNotifier(env={})   # 어떤 SLACK_WEBHOOK_*도 없음
    enabled = notifier.is_enabled()
    sent = notifier.notify_error(title="x", exc_type="NaverAuthError", message="auth")
    ok = (enabled is False) and (sent is False)
    print(f"  is_enabled={enabled} (기대 False)")
    print(f"  notify_error 반환={sent} (기대 False — 미설정 채널은 silent)")
    print(f"  [{'OK' if ok else 'FAIL'}]")
    return ok


def test_dedup() -> bool:
    """같은 알림은 throttle 창 내 1회만 — 스팸 방지."""
    print("\n=== 3) dedup/throttle ===")
    notifier, captured = make_capture_notifier(throttle_seconds=60.0)
    for _ in range(5):
        notifier.notify_error(title="dup", exc_type="NaverAuthError",
                              message="필수 환경변수 누락: NAVER_CLIENT_ID",
                              logger_name="external.naver_search")
    ok = len(captured) == 1
    print(f"  동일 알림 5회 → 캡처 {len(captured)}건 (기대 1) [{'OK' if ok else 'FAIL'}]")
    return ok


def test_redaction() -> bool:
    """API 키/토큰/시크릿이 텍스트에 새지 않는지."""
    print("\n=== 4) 비밀 스크럽 ===")
    samples = [
        ("https://apis.data.go.kr/x?serviceKey=AbCdEf123SECRET&pageNo=1",
         "AbCdEf123SECRET"),
        ("Authorization: Bearer sk-tavily-9f8e7d6c5b4a3",
         "sk-tavily-9f8e7d6c5b4a3"),
        ("KakaoAK 0123456789abcdef0123456789abcdef",
         "0123456789abcdef0123456789abcdef"),
        ('{"api_key": "TOPSECRETKEY999", "q": "강남"}',
         "TOPSECRETKEY999"),
        ("NAVER_CLIENT_SECRET=hunter2hunter2",
         "hunter2hunter2"),
    ]
    ok = True
    for raw, leaked in samples:
        scrubbed = redact(raw)
        leaked_present = leaked in scrubbed
        if leaked_present:
            ok = False
        print(f"  [{'OK ' if not leaked_present else 'FAIL'}] {raw[:48]:48s} → {scrubbed[:60]}")
    return ok


# ───────────────────────────────────────────────────────────────
# (--live) 실 external client 예외 shape 관찰 + 라우팅 결정
# ───────────────────────────────────────────────────────────────
def _observe(label: str, fn) -> None:
    """fn()을 호출해 예외를 잡고 (타입/메시지/status/라우팅 채널)을 출력."""
    try:
        fn()
        print(f"  [{label}] 예외 없음 (성공 — 정상 응답)")
    except Exception as exc:
        status = None
        for attr in ("status", "status_code"):
            v = getattr(exc, attr, None)
            if isinstance(v, int):
                status = v
                break
        channel = classify_exception(exc, logger_name=label)
        msg = redact(str(exc))
        print(f"  [{label}] {type(exc).__name__} status={status} → 채널={channel}")
        print(f"        msg: {msg[:160]}")


def live_client_shapes() -> None:
    """실 client를 BAD/GOOD 인자로 호출해 진짜 예외 shape·로그를 관찰."""
    import os

    from infra.env import load_env
    load_env()
    print("\n=== 5) 실 external client 예외 shape (--live) ===")
    print("    (BAD=잘못된/빈 키 → auth류 기대=api_limit, GOOD=실키 → 성공 또는 일반에러)")

    # ── Naver Search ──────────────────────────────────────
    print("\n  -- naver_search --")
    try:
        from infra.external.naver_search.client import NaverSearchClient
        from infra.external.naver_search.credentials import NaverCredentials, load_credentials

        # BAD: 형식상 통과하나 인증 실패할 더미 키 (8~64 영숫자)
        bad = NaverSearchClient(NaverCredentials(client_id="badbadbad1", client_secret="badbadbad1"))
        _observe("naver BAD-key", lambda: bad.get(
            "https://openapi.naver.com/v1/search/news.json", {"query": "강남", "display": 1}))

        # BAD: 자격증명 로딩 단계 — 빈 환경변수면 NaverAuthError
        def _load_empty():
            saved = {k: os.environ.pop(k, None) for k in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")}
            try:
                load_credentials()
            finally:
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v
        _observe("naver MISSING-env", _load_empty)

        # GOOD: 실키가 있으면 정상 호출
        if os.environ.get("NAVER_CLIENT_ID") and os.environ.get("NAVER_CLIENT_SECRET"):
            good = NaverSearchClient(load_credentials())
            _observe("naver GOOD-key", lambda: good.get(
                "https://openapi.naver.com/v1/search/news.json", {"query": "강남역", "display": 1}))
        else:
            print("  [naver GOOD-key] skip — NAVER_CLIENT_ID/SECRET .env 없음")
    except Exception as e:
        print(f"  naver 시험 setup 실패: {e}")

    # ── Kakao Local ───────────────────────────────────────
    print("\n  -- kakao_local --")
    try:
        from real_estate.external.kakao_local.client import KakaoLocalClient
        bad = KakaoLocalClient(rest_key="0000000000000000bad")
        _observe("kakao BAD-key", lambda: bad.search_keyword("강남역", size=1))
        good_key = os.environ.get("KAKAO_API_KEY")
        if good_key:
            _observe("kakao GOOD-key",
                     lambda: KakaoLocalClient(rest_key=good_key).search_keyword("강남역", size=1))
        else:
            print("  [kakao GOOD-key] skip — KAKAO_API_KEY .env 없음")
    except Exception as e:
        print(f"  kakao 시험 setup 실패: {e}")

    # ── Tavily ────────────────────────────────────────────
    print("\n  -- tavily --")
    try:
        from infra.external.tavily.client import TavilyClient
        _observe("tavily BAD-key",
                 lambda: TavilyClient(api_key="tvly-INVALIDKEY000").search("강남", max_results=1))
        good_key = os.environ.get("TAVILY_API_KEY")
        if good_key:
            _observe("tavily GOOD-key",
                     lambda: TavilyClient(api_key=good_key).search("강남역 상권", max_results=1))
        else:
            print("  [tavily GOOD-key] skip — TAVILY_API_KEY .env 없음")
    except Exception as e:
        print(f"  tavily 시험 setup 실패: {e}")

    # ── 공공데이터포털 (building_hub 등 family 공유) ──────
    print("\n  -- public_data_portal --")
    try:
        from real_estate.external.public_data_portal.client import PublicDataPortalClient
        endpoint = "https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo"
        bad = PublicDataPortalClient(service_key="BADSERVICEKEY000", api_name="building_hub")
        _observe("pdp BAD-key", lambda: bad.get_items(endpoint, {"sigunguCd": "11680", "bjdongCd": "10300"}))
        good_key = (os.environ.get("BUILDING_HUB_API_KEY")
                    or os.environ.get("DATA_GO_KR_API_KEY"))
        if good_key:
            good = PublicDataPortalClient(service_key=good_key, api_name="building_hub")
            _observe("pdp GOOD-key",
                     lambda: good.get_items(endpoint, {"sigunguCd": "11680", "bjdongCd": "10300",
                                                       "platGbCd": "0", "bun": "0396", "ji": "0000"}))
        else:
            print("  [pdp GOOD-key] skip — BUILDING_HUB_API_KEY/DATA_GO_KR_API_KEY .env 없음")
    except Exception as e:
        print(f"  pdp 시험 setup 실패: {e}")


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    live = "--live" in sys.argv[1:]

    results = {
        "routing": test_routing(),
        "noop": test_noop_when_absent(),
        "dedup": test_dedup(),
        "redaction": test_redaction(),
    }

    if live:
        live_client_shapes()

    print("\n=== 요약 ===")
    all_ok = all(results.values())
    for k, v in results.items():
        print(f"  {k:10s}: {'PASS' if v else 'FAIL'}")
    print(f"  {'ALL PASS' if all_ok else 'SOME FAILED'}")
    if not live:
        print("  (실 client 예외 shape는 `--live`로: python -m scripts.try_slack --live)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
