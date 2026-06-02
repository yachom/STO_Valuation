"""건축HUB Public API HTTP 클라이언트.

공공데이터포털 BldRgstHubService(건축HUB) GET 엔드포인트들이 공유하는 인증키
주입·JSON envelope 파싱·결과 코드 확인·item 추출을 한 곳에서 처리한다.
건축HUB 산하 대장(표제부/층별개요/지역지구구역)은 엔드포인트만 다르고 요청
파라미터·응답 구조가 같아 한 client로 묶을 수 있다.

로거 컨벤션: 모든 외부 호출 client는 logging.getLogger("external.{provider}")
를 사용한다. provider 이름은 폴더명과 일치 → "external.building_hub".
"""

from __future__ import annotations

import json
import logging
import time

import requests

from infra.external.building_hub.exceptions import BuildingHubError

logger = logging.getLogger("external.building_hub")


class BuildingHubClient:
    """thin requests wrapper: 인증 + 결과코드 + item 추출 + 호출 로깅."""

    def __init__(self, service_key: str, *, timeout: float = 10.0):
        self._service_key = service_key
        self._timeout = timeout

    def get_items(
        self,
        endpoint: str,
        sigungu_code: str,
        bdong_code: str,
        bun: str,
        ji: str,
    ) -> list:
        params = {
            "serviceKey": self._service_key,
            "sigunguCd": sigungu_code,
            "bjdongCd": bdong_code,
            "bun": bun,
            "ji": ji,
            "numOfRows": "100",
            "pageNo": "1",
            "_type": "json",
        }

        started_at = time.monotonic()
        try:
            response = requests.get(endpoint, params=params, timeout=self._timeout)
        except Exception as exc:
            logger.warning("GET %s -> 네트워크 오류: %s", endpoint, exc)
            raise BuildingHubError(
                f"건축HUB API 호출 실패: {exc}", endpoint=endpoint
            ) from exc

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        status = response.status_code
        body_text = response.text

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "GET %s -> JSON 파싱 실패 (%dms, status=%s)",
                endpoint, elapsed_ms, status,
            )
            raise BuildingHubError(
                "건축HUB API가 JSON이 아닌 응답을 반환했습니다. "
                "(키 오류 또는 서버 오류 가능) 응답 원문: "
                f"{body_text[:500]}",
                endpoint=endpoint,
                status=status,
            ) from exc

        header = data.get("response", {}).get("header", {})
        result_code = header.get("resultCode")
        if result_code != "00":
            result_msg = header.get("resultMsg", "알 수 없는 오류")
            logger.warning(
                "GET %s -> resultCode=%s %s (%dms)",
                endpoint, result_code, result_msg, elapsed_ms,
            )
            raise BuildingHubError(
                f"건축HUB API 오류: {result_code} {result_msg}",
                endpoint=endpoint,
                result_code=result_code,
                status=status,
            )

        body = data.get("response", {}).get("body", {})
        logger.info("GET %s -> %s (%dms)", endpoint, status, elapsed_ms)

        if str(body.get("totalCount", 0)) == "0":
            return []
        items = body.get("items", {})
        if not items:
            return []
        item = items.get("item")
        if item is None:
            return []
        return item if isinstance(item, list) else [item]
