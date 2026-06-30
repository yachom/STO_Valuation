"""음악 external 클라이언트 공용 HTTP 헬퍼 (zero-dep, urllib).

infra/external/_http.py와 같은 역할의 자산 로컬판 — 음악 클라이언트들이 공유한다.
실패(네트워크/파싱/HTTP)는 None으로 흡수한다(보강은 best-effort). 단 모든 응답·오류는
infra.external.audit로 한 곳에서 기록(로컬 jsonl)·통지(오류만 Slack 두 채널)한다 —
infra 쪽 transport와 동일한 관측 경로. api_name은 호출부가 주거나 URL 호스트에서 유도.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from infra.external import audit

logger = logging.getLogger("music_copyright.external.http")

DEFAULT_TIMEOUT = 15.0


def _host(url: str) -> str:
    """URL 호스트에서 간결한 api_name 유도 (api.genius.com → genius)."""
    try:
        netloc = urllib.parse.urlparse(url).netloc.split(":")[0]
    except Exception:  # noqa: BLE001
        return "external"
    parts = [p for p in netloc.split(".") if p not in ("api", "www", "com", "org", "net", "co", "kr")]
    return parts[0] if parts else (netloc or "external")


def get_json(url: str, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
             *, api_name: str | None = None, operation: str | None = None):
    """GET(JSON). 실패는 None으로 흡수하되 audit로 기록·통지. api_name 미지정 시 호스트 유도."""
    api = api_name or _host(url)
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    started = time.monotonic()

    def _ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        audit.record_response(api_name=api, operation=operation, status=exc.code,
                              endpoint=url, response_excerpt=body, elapsed_ms=_ms())
        logger.warning("GET 실패(계속): HTTP %s [%s]", exc.code, url[:80])
        return None
    except Exception as exc:  # noqa: BLE001 — 네트워크 등
        audit.record_response(api_name=api, operation=operation, status=None,
                              endpoint=url, error=f"{exc}", elapsed_ms=_ms())
        logger.warning("GET 실패(계속): %s [%s]", exc, url[:80])
        return None

    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — 2xx인데 JSON 아님도 오류로 기록
        audit.record_response(api_name=api, operation=operation, status=status,
                              endpoint=url, response_excerpt=raw,
                              error=f"JSON 파싱 실패: {exc}", elapsed_ms=_ms())
        logger.warning("JSON 파싱 실패(계속) [%s]", url[:80])
        return None

    audit.record_response(api_name=api, operation=operation, status=status,
                          endpoint=url, response_excerpt=raw, elapsed_ms=_ms())
    return data
