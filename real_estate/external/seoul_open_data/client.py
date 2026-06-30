"""서울 열린데이터광장(data.seoul.go.kr) thin client.

data.go.kr와 envelope이 달라 별도 client. 응답 구조:
    {"<SVC>": {"list_total_count": N,
              "row": [{...}, ...],
              "RESULT": {"CODE":"INFO-000","MESSAGE":"정상 처리되었습니다"}}}

URL 패턴:
    http://openapi.seoul.go.kr:8088/{KEY}/json/{SVC}/{START}/{END}/[{KEY1}/{KEY2}/...]

페이징은 START/END 인덱스로. 한 번에 1000건이 일반적 최대.

로거: external.seoul_open_data
"""

from __future__ import annotations

import json
import logging

import requests

from infra.external.base_client import BaseExternalClient
from infra.gateway import APIGateway

logger = logging.getLogger("external.seoul_open_data")

_BASE_URL = "http://openapi.seoul.go.kr:8088"
_API_NAME = "seoul_open_data"


class SeoulOpenDataClient(BaseExternalClient):
    api_name = _API_NAME

    def __init__(
        self,
        service_key: str,
        *,
        timeout: float = 15.0,
        gateway: APIGateway | None = None,
    ):
        super().__init__(gateway=gateway, timeout=timeout)
        self._service_key = service_key

    def fetch_page(
        self,
        service_name: str,
        *,
        start_idx: int = 1,
        end_idx: int = 1000,
        path_keys: list[str] | None = None,
    ) -> dict:
        """한 페이지를 fetch. RESULT 코드 비정상 시 RuntimeError.

        반환은 raw `{service_name: {...}}` 전체 dict. row 추출은 호출자.
        """
        parts = [_BASE_URL, self._service_key, "json", service_name,
                 str(start_idx), str(end_idx)]
        if path_keys:
            parts.extend(path_keys)
        url = "/".join(parts)

        status, text, _ = self._get_requests(
            requests, url, {},
            on_error=lambda exc: RuntimeError(f"서울 열린데이터 호출 실패: {exc}"),
            operation="fetch_page")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"서울 열린데이터가 유효한 JSON이 아님. 원문: {text[:300]}"
            ) from exc

        # 정상 응답: data[service_name]['RESULT']['CODE'] == 'INFO-000'
        # 오류 응답: data['RESULT']['CODE'] == 'INFO-100' 등
        if service_name not in data:
            if "RESULT" in data:
                code = data["RESULT"].get("CODE")
                msg = data["RESULT"].get("MESSAGE")
                raise RuntimeError(
                    f"서울 열린데이터 오류: {code} {msg}"
                )
            raise RuntimeError(f"예상치 못한 응답: {text[:300]}")

        result = data[service_name].get("RESULT", {})
        if result.get("CODE") not in ("INFO-000", "INFO-200"):
            raise RuntimeError(
                f"서울 열린데이터 오류: {result.get('CODE')} {result.get('MESSAGE')}"
            )

        logger.info(
            "GET %s [%d~%d] -> %s",
            service_name, start_idx, end_idx, status,
        )
        return data[service_name]

    def iter_rows(
        self,
        service_name: str,
        *,
        page_size: int = 1000,
        path_keys: list[str] | None = None,
    ):
        """전체 페이지 순회. row dict 단위 yield."""
        start = 1
        total = None
        while True:
            end = start + page_size - 1
            block = self.fetch_page(
                service_name, start_idx=start, end_idx=end, path_keys=path_keys,
            )
            if total is None:
                total = block.get("list_total_count", 0)
            rows = block.get("row", [])
            if not rows:
                return
            for row in rows:
                yield row
            if end >= total:
                return
            start = end + 1
