"""Naver Search Open API HTTP 클라이언트.

search/* 엔드포인트(news/blog/cafearticle/webkr/...)가 공유하는 헤더 주입·
에러코드 매핑·JSON 파싱을 한 곳에서 처리한다. Naver의 다른 family
(DataLab, Papago 등)는 응답 envelope이 달라 별도 client가 필요하므로
이 클래스는 search family에 한정한다.

로거 컨벤션: 모든 외부 호출 client는 logging.getLogger("external.{provider}")
를 사용한다. provider 이름은 폴더명과 일치 → "external.naver_search".
자산 로거("asset.{type}.{id}")와 평행 구조.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from infra.repository.external.naver_search.credentials import NaverCredentials
from infra.repository.external.naver_search.exceptions import (
    NaverAPIError,
    NaverAuthError,
    NaverBadRequest,
    NaverQuotaExceeded,
    NaverRateLimited,
)

logger = logging.getLogger("external.naver_search")

# Naver Open API 에서 관찰되는 주요 errorCode → 예외 매핑.
# 공식 명세가 완전 공개돼 있지 않으나, 문서 기반으로 정리.
_ERROR_CODE_MAP = {
    "024": NaverAuthError,        # Authentication failed
    "SE01": NaverAuthError,       # Invalid request (인증 단계에서 자주 등장)
    "010": NaverQuotaExceeded,    # Quota exceeded
    "012": NaverQuotaExceeded,    # 일일 호출 한도 초과
    "429": NaverRateLimited,
}


class NaverSearchClient:
    """thin urllib wrapper: 인증 + 에러처리 + 호출 로깅."""

    def __init__(self, credentials: NaverCredentials, *, timeout: float = 10.0):
        self.credentials = credentials
        self.timeout = timeout

    def get(self, url: str, params: dict | None = None) -> dict:
        full_url = url
        if params:
            full_url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(full_url)
        for k, v in self.credentials.as_headers().items():
            request.add_header(k, v)
        return self._send(request, label=f"GET {url}")

    def post_json(self, url: str, body: dict) -> dict:
        request = urllib.request.Request(url)
        for k, v in self.credentials.as_headers().items():
            request.add_header(k, v)
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self._send(request, data=data, label=f"POST {url}")

    def _send(self, request, *, data: bytes | None = None, label: str) -> dict:
        started_at = time.monotonic()
        try:
            with urllib.request.urlopen(request, data=data, timeout=self.timeout) as resp:
                status = resp.getcode()
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            status = exc.code
            raw = exc.read().decode("utf-8", errors="replace")
            logger.warning("%s -> HTTP %s (%dms): %s", label, status, elapsed_ms, raw[:200])
            self._raise_for_error(status, raw)
        except urllib.error.URLError as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.warning("%s -> 네트워크 오류 (%dms): %s", label, elapsed_ms, exc.reason)
            raise NaverAPIError(f"네트워크 오류: {exc.reason}") from exc

        elapsed_ms = int((time.monotonic() - started_at) * 1000)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("%s -> JSON 파싱 실패 (%dms)", label, elapsed_ms)
            raise NaverAPIError(
                "응답 JSON 파싱 실패",
                status=status,
                response_body=raw[:500],
            ) from exc

        if isinstance(parsed, dict) and parsed.get("errorCode"):
            logger.warning(
                "%s -> %s errorCode=%s (%dms)",
                label, status, parsed.get("errorCode"), elapsed_ms,
            )
            self._raise_for_error(status, raw, parsed=parsed)

        logger.info("%s -> %s (%dms)", label, status, elapsed_ms)
        return parsed

    @staticmethod
    def _raise_for_error(status: int, raw: str, *, parsed: dict | None = None) -> None:
        if parsed is None:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
        error_code = parsed.get("errorCode") if isinstance(parsed, dict) else None
        message = (
            parsed.get("errorMessage") if isinstance(parsed, dict) else None
        ) or f"Naver API 호출 실패 (HTTP {status})"

        if status == 401:
            exc_cls = NaverAuthError
        elif status == 429:
            exc_cls = NaverRateLimited
        elif status == 400:
            exc_cls = NaverBadRequest
        else:
            exc_cls = _ERROR_CODE_MAP.get(str(error_code), NaverAPIError)

        raise exc_cls(
            message,
            status=status,
            error_code=error_code,
            response_body=raw[:500],
        )
