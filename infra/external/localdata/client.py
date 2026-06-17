"""LocalData(지방행정 인허가) 조회 — 행정안전부 통합 API (apis.data.go.kr/1741000).

도로명주소(ROAD_NM_ADDR)로 상가 업종의 인허가/폐업 이력을 받아 임차 회전율
(turnover)을 산출한다. 핵심: 같은 건물 주소에서 영업/폐업 레코드가 얼마나
누적되고 평균 얼마나 존속하는가.

엔드포인트(실호출 검증, 2026-06):
    GET https://apis.data.go.kr/1741000/{service}/info
    service 예: general_restaurants(일반음식점) / rest_cafes(휴게음식점) ...
    인증: serviceKey(data.go.kr 계정 키, 데이터셋별 활용신청 필요)
    필터: cond[ROAD_NM_ADDR::LIKE]=도로명, cond[SALS_STTS_CD::EQ]=영업상태,
          cond[LCPMT_YMD::GTE/LT]=인허가일자, pageNo/numOfRows/returnType=json
    envelope: response.body.items.item[] (표준 data.go.kr)

레거시 localdata.go.kr 포털은 2026-04-16 폐쇄 → 본 통합 API로 대체.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from infra.gateway import APIGateway, get_default

logger = logging.getLogger("external.localdata")


class LocalDataError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


@dataclass
class BusinessLicense:
    """인허가 1건 정규화 — 임차 1입점 이력."""
    상호: str
    업태: str
    영업상태: str
    인허가일자: _dt.date | None
    폐업일자: _dt.date | None
    면적: float | None
    도로명주소: str
    관리번호: str

    @property
    def is_closed(self) -> bool:
        return bool(self.폐업일자) or "폐업" in (self.영업상태 or "")

    @property
    def 존속연수(self) -> float | None:
        if self.인허가일자 and self.폐업일자:
            return round((self.폐업일자 - self.인허가일자).days / 365.25, 2)
        return None


def _parse_ymd(v) -> _dt.date | None:
    s = (str(v or "")).replace("-", "").strip()
    try:
        return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8])) if len(s) >= 8 else None
    except (ValueError, TypeError):
        return None


class LocalDataClient:
    """행정안전부 인허가 통합 API thin wrapper (urllib + 게이트웨이 + 페이징)."""

    API_NAME = "localdata"
    BASE = "https://apis.data.go.kr/1741000"

    def __init__(self, service_key: str, *, timeout: float = 25.0,
                 gateway: APIGateway | None = None):
        self.service_key = service_key
        self.timeout = timeout
        self._gateway = gateway if gateway is not None else get_default()

    def fetch_by_road(self, service: str, road_address: str, *,
                      max_rows: int = 500) -> list[BusinessLicense]:
        """도로명주소로 한 업종의 인허가 이력(영업+폐업) 정규화 반환.

        Args:
            service: 엔드포인트 서비스명 (general_restaurants 등).
            road_address: 도로명 LIKE 필터 (예: "강서로 385").
            max_rows: 최대 수집 건수.
        """
        rows: list[dict] = []
        page = 1
        while len(rows) < max_rows:
            size = min(100, max_rows - len(rows))
            batch, total = self._call(service, road_address, page, size)
            rows.extend(batch)
            if not batch or len(rows) >= total:
                break
            page += 1
        return [self._normalize(r) for r in rows]

    def _call(self, service: str, road: str, page: int, size: int) -> tuple[list, int]:
        params = {
            "serviceKey": self.service_key, "pageNo": page, "numOfRows": size,
            "returnType": "json", "cond[ROAD_NM_ADDR::LIKE]": road,
        }
        url = f"{self.BASE}/{service}/info?{urllib.parse.urlencode(params)}"
        self._gateway.wait(self.API_NAME)
        try:
            raw = urllib.request.urlopen(url, timeout=self.timeout).read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise LocalDataError(f"LocalData HTTP {exc.code}: {body[:120]}",
                                 status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise LocalDataError(f"네트워크 오류: {exc.reason}") from exc
        try:
            body = json.loads(raw).get("response", {}).get("body", {})
        except json.JSONDecodeError as exc:
            raise LocalDataError(f"JSON 파싱 실패: {raw[:120]}") from exc
        items = (body.get("items") or {}).get("item") or []
        if isinstance(items, dict):
            items = [items]
        total = int(body.get("totalCount") or len(items))
        logger.info("localdata %s p%d -> %d/%d (road=%s)", service, page,
                    len(items), total, road)
        return items, total

    @staticmethod
    def _normalize(r: dict) -> BusinessLicense:
        area = r.get("LCTN_AREA") or r.get("FCLT_TOTAL_SCL")
        try:
            area = float(area) if area not in (None, "") else None
        except (ValueError, TypeError):
            area = None
        return BusinessLicense(
            상호=r.get("BPLC_NM") or "",
            업태=r.get("BZSTAT_SE_NM") or r.get("SNTTN_BZSTAT_NM") or "",
            영업상태=r.get("SALS_STTS_NM") or r.get("DTL_SALS_STTS_NM") or "",
            인허가일자=_parse_ymd(r.get("LCPMT_YMD")),
            폐업일자=_parse_ymd(r.get("CLSBIZ_YMD")),
            면적=area,
            도로명주소=r.get("ROAD_NM_ADDR") or "",
            관리번호=r.get("MNG_NO") or "",
        )
