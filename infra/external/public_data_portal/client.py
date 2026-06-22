"""공공데이터포털(data.go.kr) GET 엔드포인트 공통 HTTP 클라이언트.

공공데이터포털 산하 다수의 OpenAPI(BldRgstHubService, 인구결제정보, 에너지효율
등급 등)는 다음 envelope을 공유한다:
    - 쿼리 파라미터: serviceKey, numOfRows, pageNo, _type
    - 응답:         response.header.resultCode / response.body.items.item

이 클래스는 envelope 처리(인증키 주입·JSON 파싱·결과코드 확인·item 추출)를
한 곳에서 책임진다. 엔드포인트 URL과 엔드포인트별 요청 파라미터는 호출자
(repository)가 결정한다.

여러 API family가 공공데이터포털을 공유하지만 quota는 family별로 적용되므로,
client 인스턴스는 한 family당 하나를 두는 게 안전하다. 생성 시 api_name을
받아 게이트웨이 rate-limit 키와 일치시킨다.

로거 컨벤션: external.{api_name} (예: external.building_hub)
"""

from __future__ import annotations

import logging

import requests

from infra.external import _http
from infra.external.public_data_portal.exceptions import PublicDataPortalError
from infra.gateway import APIGateway, get_default


class PublicDataPortalClient:
    """thin requests wrapper: 인증 + envelope 파싱 + item 추출 + 게이트웨이 통과."""

    def __init__(
        self,
        service_key: str,
        api_name: str,
        *,
        timeout: float = 10.0,
        gateway: APIGateway | None = None,
    ):
        self._service_key = service_key
        self._api_name = api_name
        self._timeout = timeout
        self._gateway = gateway if gateway is not None else get_default()
        self._logger = logging.getLogger(f"external.{api_name}")

    def get_items(self, endpoint: str, params: dict) -> list:
        merged = {
            "serviceKey": self._service_key,
            "numOfRows": "100",
            "pageNo": "1",
            "_type": "json",
            **params,
        }

        def _on_net_error(exc: Exception) -> BaseException:
            self._logger.warning("GET %s -> 네트워크 오류: %s", endpoint, exc)
            return PublicDataPortalError(
                f"공공데이터포털 호출 실패: {exc}",
                api_name=self._api_name,
                endpoint=endpoint,
            )

        status, body_text, elapsed_ms = _http.timed_requests_get(
            requests, endpoint, merged,
            api_name=self._api_name,
            gateway=self._gateway,
            timeout=self._timeout,
            on_error=_on_net_error,
        )

        def _on_json_error(exc):
            self._logger.warning(
                "GET %s -> JSON 파싱 실패 (%dms, status=%s)",
                endpoint, elapsed_ms, status,
            )
            return PublicDataPortalError(
                "공공데이터포털이 JSON이 아닌 응답을 반환했습니다. "
                "(키 오류 또는 서버 오류 가능) 응답 원문: "
                f"{body_text[:500]}",
                api_name=self._api_name,
                endpoint=endpoint,
                status=status,
            )

        data = _http.parse_json(body_text, on_error=_on_json_error)

        header = data.get("response", {}).get("header", {})
        result_code = header.get("resultCode")
        if result_code != "00":
            result_msg = header.get("resultMsg", "알 수 없는 오류")
            self._logger.warning(
                "GET %s -> resultCode=%s %s (%dms)",
                endpoint, result_code, result_msg, elapsed_ms,
            )
            raise PublicDataPortalError(
                f"공공데이터포털 API 오류: {result_code} {result_msg}",
                api_name=self._api_name,
                endpoint=endpoint,
                result_code=result_code,
                status=status,
            )

        body = data.get("response", {}).get("body", {})
        self._logger.info("GET %s -> %s (%dms)", endpoint, status, elapsed_ms)

        if str(body.get("totalCount", 0)) == "0":
            return []
        items = body.get("items", {})
        if not items:
            return []
        item = items.get("item")
        if item is None:
            return []
        return item if isinstance(item, list) else [item]
