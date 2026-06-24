"""외부 HTTP client 공통 transport 헬퍼.

여러 external/*/client.py 가 똑같이 반복하던 골격
    gateway.wait(api_name) → started=time.monotonic() → 요청 →
    except (HTTPError/URLError | requests 예외) → elapsed_ms → (json/xml 파싱)
만 이 모듈로 모은다. **transport·timing·게이트웨이·파싱 plumbing 한정**이고,
도메인 의미(에러코드 매핑·예외 클래스·envelope 해석)는 각 client가 그대로 보유한다.

설계 원칙:
- client별 예외는 client가 만든다. 헬퍼는 `on_*` 콜백을 호출해 client가
  자기 예외를 raise 하도록 한다(헬퍼는 client 예외 타입을 모른다).
- 로거 이름(`external.{provider}`)·게이트웨이 사용·반환 shape는 호출부가 결정.
- urllib family(naver/kakao/tavily/localdata/reb_rone)와 requests family
  (public_data_portal/molit_rtms)는 라이브러리·예외 타입이 달라 별도 함수로 둔다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

__all__ = [
    "timed_urlopen",
    "timed_requests_get",
    "parse_json",
    "paginate",
]


def _notify_api_error(api_name: str, status: int | None, detail: str) -> None:
    """외부 API 오류를 Slack에 결정론적 통지 (LLM 무개입). 모든 외부 HTTP의 공통
    경유점이라 여기 한 곳에서 4xx/5xx·네트워크 오류를 빠짐없이 잡는다. Slack 미설정/
    실패 시 조용히 넘어가며 transport를 절대 깨지 않는다(best-effort). 분류(인증/키/
    레이트리밋 401·403·429 → api-limit 채널, 그 외 → alerts)·dedup은 notifier가 한다."""
    try:
        from infra.notify.slack import get_notifier
        n = get_notifier()
        if n is not None:
            n.notify_error(title=f"외부 API 오류: {api_name}", message=detail,
                           status=status, logger_name=f"external.{api_name}",
                           source=api_name, level="ERROR")
    except Exception:
        pass


def timed_urlopen(
    request,
    *,
    api_name: str,
    gateway,
    timeout: float,
    on_http_error: Callable[[int, str], BaseException],
    on_url_error: Callable[[str], BaseException],
    data: bytes | None = None,
) -> tuple[int, str, int]:
    """게이트웨이 통과 후 urllib 요청을 보내고 (status, body, elapsed_ms) 반환.

    `request`는 urllib.request.Request. POST면 `data`(bytes)를 준다.
    transport 실패 시 client가 만든 예외를 raise 한다:
        - urllib.error.HTTPError → on_http_error(status, body) 가 만든 예외
        - urllib.error.URLError  → on_url_error(reason) 가 만든 예외
    """
    gateway.wait(api_name)
    started_at = time.monotonic()
    try:
        with urllib.request.urlopen(request, data=data, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
        _notify_api_error(api_name, status, body[:300])
        raise on_http_error(status, body) from exc
    except urllib.error.URLError as exc:
        _notify_api_error(api_name, None, f"네트워크 오류: {exc.reason}")
        raise on_url_error(str(exc.reason)) from exc

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return status, raw, elapsed_ms


def timed_requests_get(
    requests_module,
    endpoint: str,
    params: dict,
    *,
    api_name: str,
    gateway,
    timeout: float,
    on_error: Callable[[Exception], BaseException],
) -> tuple[int, str, int]:
    """게이트웨이 통과 후 requests.get을 보내고 (status, text, elapsed_ms) 반환.

    `requests_module`은 호출부가 import한 requests (헬퍼는 requests를 import하지
    않는다 — 호출부 import 스타일/주입 보존). 네트워크 예외(requests가 던지는
    모든 것)는 on_error(exc) 가 만든 client 예외로 변환한다.
    """
    gateway.wait(api_name)
    started_at = time.monotonic()
    try:
        response = requests_module.get(endpoint, params=params, timeout=timeout)
    except Exception as exc:  # requests.RequestException 포함 — 호출부 동작 보존
        _notify_api_error(api_name, None, f"네트워크 오류: {exc}"[:300])
        raise on_error(exc) from exc

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    if response.status_code >= 400:   # requests 계열은 4xx/5xx가 예외 아님 — 여기서 결정론적 통지
        _notify_api_error(api_name, response.status_code, response.text[:300])
    return response.status_code, response.text, elapsed_ms


def parse_json(raw: str, *, on_error: Callable[[json.JSONDecodeError], BaseException]):
    """json.loads(raw). 실패 시 on_error(exc) 가 만든 client 예외를 raise.

    `on_error`는 client가 로깅/메시지/status 등을 채운 자기 예외를 만든다.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise on_error(exc) from exc


def paginate(
    fetch_page: Callable[[int, int], tuple[list, int]],
    *,
    max_rows: int,
    page_size: int,
) -> list:
    """`(batch, total)`을 돌려주는 fetch_page(page, size)를 누적 호출.

    1-base page. 남은 한도에 맞춰 size를 줄이고, 빈 배치나 total 도달 시 종료.
    localdata/reb_rone가 공유하던 루프 그대로.
    """
    rows: list = []
    page = 1
    while len(rows) < max_rows:
        size = min(page_size, max_rows - len(rows))
        batch, total = fetch_page(page, size)
        rows.extend(batch)
        if not batch or len(rows) >= total:
            break
        page += 1
    return rows
