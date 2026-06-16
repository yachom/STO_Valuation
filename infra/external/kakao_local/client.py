"""Kakao Local 장소 검색 HTTP 클라이언트 (키워드 / 카테고리).

dapi.kakao.com/v2/local/search/{keyword,category}.json 을 thin urllib로 감싼다.
인증은 `Authorization: KakaoAK {rest_key}` 헤더 하나뿐이라(네이버처럼 id+secret
credentials 객체가 불필요), defer-base 원칙에 따라 별도 credentials/exceptions
모듈 없이 이 파일에 최소로 둔다.

응답은 카카오 원형(dict: documents[] + meta)을 그대로 반환한다. 도메인 해석
(건물 필터·테넌트 정규화)은 상위 repository가 담당한다.

로거: external.kakao_local (cf. infra/external/naver_search/client.py).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from infra.gateway import APIGateway, get_default

logger = logging.getLogger("external.kakao_local")


class KakaoLocalError(RuntimeError):
    """Kakao Local 호출 실패 (인증·쿼터·네트워크·파싱)."""

    def __init__(self, message, *, status=None, response_body=None):
        super().__init__(message)
        self.status = status
        self.response_body = response_body


class KakaoLocalClient:
    """thin urllib wrapper: KakaoAK 인증 + 게이트웨이 + 호출 로깅."""

    API_NAME = "kakao_local"
    KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
    CATEGORY_URL = "https://dapi.kakao.com/v2/local/search/category.json"

    def __init__(self, rest_key: str, *, timeout: float = 10.0,
                 gateway: APIGateway | None = None):
        self.rest_key = rest_key
        self.timeout = timeout
        self._gateway = gateway if gateway is not None else get_default()

    def search_keyword(self, query: str, *, x: float | None = None,
                       y: float | None = None, radius: int | None = None,
                       page: int = 1, size: int = 15,
                       sort: str | None = None) -> dict:
        """키워드 장소 검색 (예: 건물명). x/y/radius 주면 좌표 중심 우선 정렬.

        radius: 미터(0~20000). sort: 'accuracy'|'distance'.
        반환: 카카오 원형 {documents:[...], meta:{...}}.
        """
        params: dict = {"query": query, "page": page, "size": size}
        if x is not None and y is not None:
            params["x"], params["y"] = x, y
            if radius is not None:
                params["radius"] = radius
        if sort:
            params["sort"] = sort
        return self._get(self.KEYWORD_URL, params)

    def search_category(self, category_group_code: str, *, x: float, y: float,
                        radius: int = 100, page: int = 1, size: int = 15,
                        sort: str | None = None) -> dict:
        """카테고리 그룹 코드로 좌표 반경 검색 (FD6 음식점, CE7 카페, HP8 병원 등).

        radius: 미터(0~20000). 반환: 카카오 원형.
        """
        params: dict = {"category_group_code": category_group_code,
                        "x": x, "y": y, "radius": radius,
                        "page": page, "size": size}
        if sort:
            params["sort"] = sort
        return self._get(self.CATEGORY_URL, params)

    def _get(self, url: str, params: dict) -> dict:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(full_url)
        request.add_header("Authorization", "KakaoAK " + self.rest_key)
        label = f"GET {url.rsplit('/', 1)[-1]}"

        self._gateway.wait(self.API_NAME)
        started_at = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                status = resp.getcode()
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", errors="replace")
            logger.warning("%s -> HTTP %s: %s", label, status, raw[:200])
            raise KakaoLocalError(f"Kakao Local 호출 실패 (HTTP {status})",
                                  status=status, response_body=raw[:500]) from exc
        except urllib.error.URLError as exc:
            logger.warning("%s -> 네트워크 오류: %s", label, exc.reason)
            raise KakaoLocalError(f"네트워크 오류: {exc.reason}") from exc

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KakaoLocalError("응답 JSON 파싱 실패", status=status,
                                  response_body=raw[:500]) from exc
        n = len((parsed.get("documents") or [])) if isinstance(parsed, dict) else 0
        logger.info("%s -> %s (%dms, %d건)", label, status, elapsed_ms, n)
        return parsed
