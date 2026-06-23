"""음악 external 클라이언트 공용 HTTP 헬퍼 (zero-dep, urllib).

infra/external/_http.py와 같은 역할의 자산 로컬판 — 음악 클라이언트들이 공유한다.
실패(네트워크/파싱/HTTP)는 None으로 흡수한다(보강은 best-effort).
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger("music_copyright.external.http")

DEFAULT_TIMEOUT = 15.0


def get_json(url: str, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT):
    """GET(JSON). 실패는 None으로 흡수."""
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET 실패(계속 진행): %s [%s]", exc, url[:80])
        return None
