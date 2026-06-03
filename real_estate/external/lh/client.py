"""LH 공공임대주택 공급정보 OpenAPI 클라이언트.

공공데이터포털 산하지만 LH 전용 envelope을 쓰며 (`B552555` family),
표준 `PublicDataPortalClient`와 약간 다르다. 응답 구조:

    {"dsList":[{"dsId":"...","dsContents":[{...}, ...]}], "resCnt":N, ...}

또는 일부 endpoint은
    {"response":{"header":{...},"body":{"items":[{...}]}}}

family와 quota는 `LH_PUBLIC_HOUSING_KEY` 1개로 묶여 운용.
"""

from __future__ import annotations

import json
import logging
import time

import requests

from infra.gateway import APIGateway, get_default

logger = logging.getLogger("external.lh_public_housing")


class LhClient:
    """LH OpenAPI thin wrapper. endpoint별 응답 형태가 살짝 달라 호출자가
    원본 dict를 받아 정규화한다.
    """

    def __init__(
        self,
        service_key: str,
        *,
        timeout: float = 15.0,
        gateway: APIGateway | None = None,
    ):
        self._service_key = service_key
        self._api_name = "lh_public_housing"
        self._timeout = timeout
        self._gateway = gateway if gateway is not None else get_default()

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        merged = {
            "serviceKey": self._service_key,
            "PAGE": "1",
            "PER_PAGE": "1000",
            "_type": "json",
            **(params or {}),
        }
        self._gateway.wait(self._api_name)
        started_at = time.monotonic()
        try:
            response = requests.get(endpoint, params=merged, timeout=self._timeout)
        except Exception as exc:
            logger.warning("GET %s -> 네트워크 오류: %s", endpoint, exc)
            raise RuntimeError(f"LH 호출 실패: {exc}") from exc

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"LH가 JSON이 아닌 응답을 반환했습니다. 원문: {response.text[:300]}"
            ) from exc

        logger.info(
            "GET %s -> %s (%dms)",
            endpoint, response.status_code, elapsed_ms,
        )
        return data

    @staticmethod
    def extract_items(body: dict) -> list[dict]:
        """LH의 두 가지 응답 형태에서 item list만 골라낸다."""
        # 케이스 1: data.go.kr 표준 envelope
        try:
            items = body["response"]["body"]["items"]
            if isinstance(items, dict):
                inner = items.get("item")
                if inner is None:
                    return []
                return inner if isinstance(inner, list) else [inner]
            if isinstance(items, list):
                return items
        except (KeyError, TypeError):
            pass
        # 케이스 2: LH 자체 envelope
        ds_list = body.get("dsList")
        if isinstance(ds_list, list) and ds_list:
            first = ds_list[0]
            contents = first.get("dsContents")
            if isinstance(contents, list):
                return contents
        return []
