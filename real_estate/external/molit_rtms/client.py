"""국토교통부 RTMS(아파트 실거래/전월세) OpenAPI 공통 클라이언트.

공공데이터포털 산하지만 envelope은 BEEC/GSEED와 다른 XML 응답이고,
resultCode도 "000"(success) 컨벤션을 쓴다. 그래서 PublicDataPortalClient
와 분리해 RE 도메인 어댑터로 둔다.

지원 endpoint family:
    - 아파트 매매 실거래 (getRTMSDataSvcAptTrade)
    - 아파트 전월세 실거래 (getRTMSDataSvcAptRent)

요청 envelope:
    serviceKey, LAWD_CD(시군구 5자리), DEAL_YMD(YYYYMM), pageNo, numOfRows

응답 envelope (XML):
    <response>
      <header><resultCode>000</resultCode><resultMsg>...</resultMsg></header>
      <body>
        <items><item>...</item><item>...</item></items>
        <numOfRows>...</numOfRows>
        <pageNo>...</pageNo>
        <totalCount>...</totalCount>
      </body>
    </response>

같은 family 안에서 quota가 공유되므로 client 인스턴스는 family당 1개를 권장한다.
gateway rate-limit 키는 api_name과 일치 (예: "molit_real_transaction").

로거 컨벤션: external.{api_name}
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

from infra.external import _http
from infra.gateway import APIGateway, get_default
from real_estate.external.molit_rtms.exceptions import MolitRtmsError

_SUCCESS_CODES = {"000", "00", "0"}


class MolitRtmsClient:
    """RTMS XML envelope 전용 thin client.

    엔드포인트 URL과 endpoint별 요청 파라미터는 호출자(빌더/repository)가 결정한다.
    """

    def __init__(
        self,
        service_key: str,
        api_name: str,
        *,
        timeout: float = 15.0,
        gateway: APIGateway | None = None,
    ):
        self._service_key = service_key
        self._api_name = api_name
        self._timeout = timeout
        self._gateway = gateway if gateway is not None else get_default()
        self._logger = logging.getLogger(f"external.{api_name}")

    def get_items(
        self,
        endpoint: str,
        *,
        lawd_cd: str,
        deal_ymd: str,
        page_no: int = 1,
        num_of_rows: int = 1000,
        extra_params: dict | None = None,
    ) -> list[dict]:
        """한 페이지의 거래 item을 dict 리스트로 반환한다.

        Args:
            endpoint: 전체 URL.
            lawd_cd: 시군구 5자리 코드 (LAWD_CD).
            deal_ymd: 거래연월 YYYYMM.
            page_no: 1-base 페이지 번호.
            num_of_rows: 페이지 크기. RTMS 권장 최대 1000.
            extra_params: 모든 endpoint family가 공유하지 않는 부가 파라미터.

        Returns:
            item 노드 1개를 dict {tag: text} 형태로 변환한 리스트.
            거래가 0건이면 빈 리스트.

        Raises:
            MolitRtmsError: 네트워크 오류, XML 파싱 실패, resultCode != "000".
        """
        params = {
            "serviceKey": self._service_key,
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "pageNo": str(page_no),
            "numOfRows": str(num_of_rows),
        }
        if extra_params:
            params.update({k: str(v) for k, v in extra_params.items()})

        def _on_net_error(exc: Exception) -> BaseException:
            self._logger.warning("GET %s -> 네트워크 오류: %s", endpoint, exc)
            return MolitRtmsError(
                f"MOLIT RTMS 호출 실패: {exc}",
                api_name=self._api_name,
                endpoint=endpoint,
            )

        status, body_text, elapsed_ms = _http.timed_requests_get(
            requests, endpoint, params,
            api_name=self._api_name,
            gateway=self._gateway,
            timeout=self._timeout,
            on_error=_on_net_error,
        )

        try:
            root = ET.fromstring(body_text)
        except ET.ParseError as exc:
            self._logger.warning(
                "GET %s -> XML 파싱 실패 (%dms, status=%s)",
                endpoint, elapsed_ms, status,
            )
            raise MolitRtmsError(
                "MOLIT RTMS가 유효한 XML이 아닌 응답을 반환했습니다. "
                "(키 오류 또는 서버 오류 가능) 응답 원문: "
                f"{body_text[:500]}",
                api_name=self._api_name,
                endpoint=endpoint,
                status=status,
            ) from exc

        header = root.find("header") or root.find(".//header")
        result_code = (header.findtext("resultCode") if header is not None else None) or ""
        if result_code not in _SUCCESS_CODES:
            result_msg = header.findtext("resultMsg") if header is not None else "알 수 없는 오류"
            self._logger.warning(
                "GET %s -> resultCode=%s %s (%dms)",
                endpoint, result_code, result_msg, elapsed_ms,
            )
            raise MolitRtmsError(
                f"MOLIT RTMS API 오류: {result_code} {result_msg}",
                api_name=self._api_name,
                endpoint=endpoint,
                result_code=result_code,
                status=status,
            )

        self._logger.info(
            "GET %s LAWD_CD=%s DEAL_YMD=%s -> %s (%dms)",
            endpoint, lawd_cd, deal_ymd, status, elapsed_ms,
        )

        items_node = root.find(".//items")
        if items_node is None:
            return []
        return [_xml_item_to_dict(item) for item in items_node.findall("item")]

    def iter_items(
        self,
        endpoint: str,
        *,
        lawd_cd: str,
        deal_ymd: str,
        page_size: int = 1000,
        extra_params: dict | None = None,
    ):
        """모든 페이지를 순차 yield. 페이지 수 자동 추정."""
        page_no = 1
        while True:
            batch = self.get_items(
                endpoint,
                lawd_cd=lawd_cd,
                deal_ymd=deal_ymd,
                page_no=page_no,
                num_of_rows=page_size,
                extra_params=extra_params,
            )
            if not batch:
                return
            for item in batch:
                yield item
            if len(batch) < page_size:
                return
            page_no += 1


def _xml_item_to_dict(item: ET.Element) -> dict[str, str]:
    """<item> 자식 element들을 {tag: stripped_text} dict로 변환."""
    result: dict[str, str] = {}
    for child in item:
        text = (child.text or "").strip()
        result[child.tag] = text
    return result
