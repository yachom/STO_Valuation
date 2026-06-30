"""모든 외부 API client의 얇은 공통 베이스 — 게이트웨이 + audit(전수 기록·오류 알림).

서브클래스는 `api_name`(클래스 속성)만 정하면, 외부 호출이 한 곳(infra.external.audit)에서
기록(로컬 responses.jsonl)·통지(오류만 Slack 두 채널)된다. 호출 형태별 헬퍼:

    - HTTP(requests): _get_requests(...)  → infra.external._http.timed_requests_get 경유
    - HTTP(urllib):   _urlopen(...)       → infra.external._http.timed_urlopen 경유
    - 라이브러리(ddgs/yt-dlp 등 비-HTTP): with self._audited(operation): ...

이미 _http transport를 직접 쓰는 client(naver/tavily/kakao/…)도 그대로 audit 경유라,
이 베이스는 '미경유 client(ddgs/youtube/lh/seoul) 흡수 + 점진 통일'용이다.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from infra.external import _http, audit
from infra.gateway import APIGateway, get_default


class BaseExternalClient:
    """외부 client 공통 베이스. `api_name`을 서브클래스가 정의."""

    api_name: str = "external"

    def __init__(self, *, gateway: APIGateway | None = None, timeout: float = 15.0):
        self._gateway = gateway if gateway is not None else get_default()
        self._timeout = timeout

    # ── 라이브러리/비-HTTP 호출 래핑 ──────────────────────────
    @contextmanager
    def _audited(self, operation: str, *, endpoint: str | None = None,
                 request_summary=None):
        """비-HTTP 호출(서드파티 라이브러리 등)을 감싸 성공/예외를 audit에 기록한다.

        status를 모르므로 정상 종료=200 가정, 예외=오류(두 채널 통지). 게이트웨이
        wait는 서브클래스가 호출 직전에 직접 수행(라이브러리마다 호출 시점이 달라서)."""
        started = time.monotonic()
        try:
            yield
        except Exception as exc:
            audit.record_response(
                api_name=self.api_name, operation=operation, status=None,
                endpoint=endpoint, request_summary=request_summary,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000))
            raise
        audit.record_response(
            api_name=self.api_name, operation=operation, status=200,
            endpoint=endpoint, request_summary=request_summary,
            elapsed_ms=int((time.monotonic() - started) * 1000))

    # ── HTTP 헬퍼 (audit는 transport 내부에서 자동) ───────────
    def _get_requests(self, requests_module, endpoint: str, params: dict, *,
                      on_error, operation: str | None = None):
        return _http.timed_requests_get(
            requests_module, endpoint, params, api_name=self.api_name,
            gateway=self._gateway, timeout=self._timeout,
            on_error=on_error, operation=operation)

    def _urlopen(self, request, *, on_http_error, on_url_error,
                 data: bytes | None = None, operation: str | None = None):
        return _http.timed_urlopen(
            request, api_name=self.api_name, gateway=self._gateway,
            timeout=self._timeout, on_http_error=on_http_error,
            on_url_error=on_url_error, data=data, operation=operation)
