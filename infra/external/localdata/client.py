"""LOCALDATA(지방행정 인허가데이터) openDataApi HTTP 클라이언트 (urllib).

목적
====
도로명주소(RDNWHLADDR) 단위로 상가 업종의 **인허가/폐업 이력**을 가져와
임차 회전율(turnover) 산출에 쓴다. 핵심은 같은 주소에서 영업/폐업 레코드가
얼마나 누적되는가다.

엔드포인트 현황 (2026-06 기준, 실제 호출로 확인)
=================================================
- 레거시 포털 localdata.go.kr 는 **2026-04-16 0시부로 폐쇄**되었다.
  레거시 openDataApi 엔드포인트
      http(s)://www.localdata.go.kr/platform/rest/{API}/openDataApi
  는 현재 Connection refused(서버 다운). 따라서 이 URL로는 호출 불가.
- 데이터는 공공데이터포털 data.go.kr 로 이전됨. 단 현 시점 data.go.kr 노출 형태는
  주로 **파일데이터(월 단위 CSV)** 와 **표준데이터**:
      * 행정안전부_일반음식점 (fileData 15045016)
      * 전국일반음식점표준데이터 (standard 15096283)
        CSV: https://file.localdata.go.kr/file/general_restaurants/info
  레거시와 동일한 per-row openDataApi(REST, JSON/XML)를 data.go.kr serviceKey로
  바로 부르는 통합 엔드포인트는 아직 공개 확인되지 않음(활용신청 후 발급되는
  엔드포인트 URL을 BASE 로 주입해 쓰는 구조로 설계).

키 요구사항
===========
- 레거시: localdata.go.kr 개발센터 "API신청"에서 그룹/업종 선택 후 발급된 authKey.
- 현행: data.go.kr 해당 데이터셋 "활용신청" → serviceKey(=authKey 역할) 발급.
  (.env 에 LOCALDATA 키 부재 — LOCALDATA_API_KEY 신규 추가 필요.)
이 client는 키 이름에 종속되지 않게 생성자에서 auth_key 문자열만 받는다.
엔드포인트가 복구/이전되면 base_url 만 바꿔 끼우면 동작하도록 작성.

레거시 envelope (openDataApi, resultType=json) — 본 client가 파싱하는 형태
========================================================================
    {
      "result": {
        "header": {
          "process": {"code": "00", "message": "정상"},
          "paging": {"totalCount": N, "pageIndex": 1, "pageSize": 500},
          ...
        },
        "body": {
          "rows": [ { "row": [ {필드...}, ... ] } ]
        }
      }
    }
레코드(row) 주요 필드:
    opnSvcId/opnSvcNm  업종ID/업종명         mgtNo            관리번호(키)
    apvPermYmd         인허가일자           apvCancelYmd     인허가취소일자
    trdStateGbn/Nm     영업상태코드/명       dtlStateGbn/Nm   상세영업상태코드/명
    dcbYmd             폐업일자             clgStdt/clgEnddt 휴업시작/종료
    ropnYmd            재개업일자
    siteWhlAddr        지번주소             rdnWhlAddr       도로명주소(★bd_mgt_sn 매칭)
    bplcNm             사업장명             siteTel          전화
    x / y              좌표 (TM, EPSG:5174) → WGS84 변환 필요(아래 메모)

영업상태(trdStateNm) 주의
=========================
trdStateGbn 코드(01~06)의 숫자→의미 매핑은 업종군마다 표기가 엇갈려(예: 일부 가이드는
01=영업/정상·02=폐업·03=휴업, 일부 표준데이터는 다른 부여) 코드 숫자로 폐업을 단정하면
오분류 위험이 있다. 따라서 본 client는 **trdStateNm 텍스트("폐업" 포함 여부)** 로
폐업을 판정하고, 코드·명·폐업일자(dcbYmd)를 모두 보존해 호출자가 재판정 가능하게 둔다.

좌표 변환 메모
==============
x/y 는 TM 중부원점 계열(통상 EPSG:5174). 지도/거리계산 전 WGS84(EPSG:4326)로 변환 필요.
pyproj Transformer(5174→4326) 사용. bd_mgt_sn 매칭은 좌표가 아니라 rdnWhlAddr(도로명)
정규화 후 주소 조인으로 하는 것이 1차. 좌표는 보조 검증용.

로거 컨벤션: external.localdata
게이트웨이 키:  localdata  (infra/gateway/rate_limits.json 에 등록)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from infra.gateway import APIGateway, get_default

logger = logging.getLogger("external.localdata")

API_NAME = "localdata"

# 레거시 LOCALDATA openDataApi 베이스. 포털 폐쇄 후 다운 상태이므로 기본값으로만 두고,
# data.go.kr 이전 엔드포인트가 확정되면 생성자 base_url 로 주입해 교체한다.
_LEGACY_BASE = "https://www.localdata.go.kr/platform/rest"

# data.go.kr 표준데이터(전국일반음식점) 월간 CSV — openDataApi 미동작 시 폴백 수집원.
STANDARD_CSV_URL = "https://file.localdata.go.kr/file/general_restaurants/info"

# 업종ID(opnSvcId) 예시 — 상가 회전율에 의미 있는 업종군.
# (정확한 코드는 활용신청 시 업종 목록에서 확인. 아래는 레거시 포털 표기 기준 참고값.)
OPN_SVC_IDS = {
    "general_restaurant": "07_24_04_P",   # 일반음식점
    "rest_restaurant": "07_24_05_P",      # 휴게음식점
    "retail": "11_45_02_P",               # 소매(예시)
    "tele_sales": "01_01_06_P",           # 통신판매업(예시)
}

# 폐업 판정에 쓰는 영업상태명 토큰(코드 대신 텍스트 사용 — 위 docstring 참고).
CLOSED_STATE_TOKENS = ("폐업", "직권말소", "말소")


@dataclass
class BusinessLicense:
    """정규화된 인허가/폐업 레코드 1건."""

    mgt_no: str | None            # 관리번호 (업종ID+자치단체코드와 함께 자연키)
    opn_svc_id: str | None        # 업종ID
    opn_svc_nm: str | None        # 업종명
    bplc_nm: str | None           # 사업장명
    rdn_whl_addr: str | None      # 도로명전체주소 (bd_mgt_sn 매칭용)
    site_whl_addr: str | None     # 지번전체주소
    trd_state_gbn: str | None     # 영업상태코드 (원본 보존)
    trd_state_nm: str | None      # 영업상태명
    apv_perm_ymd: str | None      # 인허가일자 (YYYYMMDD)
    dcb_ymd: str | None           # 폐업일자 (YYYYMMDD)
    x: str | None                 # TM x (EPSG:5174 추정)
    y: str | None                 # TM y (EPSG:5174 추정)
    last_mod_ts: str | None       # 최종수정일시

    @property
    def is_closed(self) -> bool:
        nm = self.trd_state_nm or ""
        if any(tok in nm for tok in CLOSED_STATE_TOKENS):
            return True
        # 상태명이 비어도 폐업일자가 있으면 폐업으로 본다(보수적 보강).
        return bool(self.dcb_ymd and self.dcb_ymd.strip())


class LocalDataError(RuntimeError):
    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class LocalDataClient:
    """LOCALDATA openDataApi thin wrapper: 인증 + 페이징 + envelope 파싱 + 정규화.

    엔드포인트가 살아있을 때 동작한다. base_url 을 주입하면 data.go.kr 이전
    엔드포인트로 교체 가능. 페이지당 최대 500건(서버 제한).
    """

    MAX_PAGE_SIZE = 500

    def __init__(
        self,
        auth_key: str,
        *,
        base_url: str = _LEGACY_BASE,
        api_segment: str = "TO0",   # openDataApi 앞 segment (업종군/포맷에 따라 상이)
        timeout: float = 20.0,
        gateway: APIGateway | None = None,
    ):
        self._auth_key = auth_key
        self._base_url = base_url.rstrip("/")
        self._api_segment = api_segment
        self._timeout = timeout
        self._gateway = gateway if gateway is not None else get_default()

    # ── 공개 API ──────────────────────────────────────────────────
    def fetch_licenses(
        self,
        *,
        opn_svc_id: str,
        local_code: str | None = None,
        last_mod_bgn: str | None = None,   # YYYYMMDD
        last_mod_end: str | None = None,   # YYYYMMDD
        max_rows: int = 2000,
    ) -> list[BusinessLicense]:
        """업종(opn_svc_id) + 지역(local_code) + 갱신기간으로 인허가 레코드를 가져온다.

        도로명주소로 좁히는 법: LOCALDATA openDataApi는 주소 직접 필터가 없으므로
        local_code(자치단체 6자리)로 시군구를 좁힌 뒤, 반환된 rdn_whl_addr 를
        호출자가 도로명/건물 단위로 필터한다(예: '마곡' 포함).
        """
        rows: list[dict] = []
        page = 1
        while len(rows) < max_rows:
            size = min(self.MAX_PAGE_SIZE, max_rows - len(rows))
            batch, total = self._call_page(
                opn_svc_id=opn_svc_id,
                local_code=local_code,
                last_mod_bgn=last_mod_bgn,
                last_mod_end=last_mod_end,
                page=page,
                size=size,
            )
            rows.extend(batch)
            if not batch or len(rows) >= total:
                break
            page += 1
        return [self._normalize(r) for r in rows]

    def fetch_closed(self, **kwargs) -> list[BusinessLicense]:
        """fetch_licenses 결과 중 폐업(trd_state_nm='폐업'…)만 반환."""
        return [r for r in self.fetch_licenses(**kwargs) if r.is_closed]

    # ── 내부 ──────────────────────────────────────────────────────
    def _call_page(
        self, *, opn_svc_id, local_code, last_mod_bgn, last_mod_end, page, size
    ) -> tuple[list[dict], int]:
        params = {
            "authKey": self._auth_key,
            "resultType": "json",
            "opnSvcId": opn_svc_id,
            "pageIndex": str(page),
            "pageSize": str(size),
        }
        if local_code:
            params["localCode"] = local_code
        if last_mod_bgn:
            params["lastModTsBgn"] = last_mod_bgn
        if last_mod_end:
            params["lastModTsEnd"] = last_mod_end

        url = f"{self._base_url}/{self._api_segment}/openDataApi?{urllib.parse.urlencode(params)}"
        self._gateway.wait(API_NAME)
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raise LocalDataError(f"LOCALDATA HTTP {exc.code}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise LocalDataError(
                f"LOCALDATA 네트워크 오류(엔드포인트 폐쇄 가능성): {exc.reason}"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LocalDataError(
                "LOCALDATA가 JSON이 아닌 응답을 반환(키 오류/서버 오류 가능): "
                f"{raw[:300]}"
            ) from exc

        result = data.get("result", {})
        header = result.get("header", {})
        process = header.get("process", {})
        code = process.get("code")
        if code is not None and code not in ("00", "000"):
            raise LocalDataError(
                f"LOCALDATA API 오류: {code} {process.get('message')}", code=code,
                status=status,
            )

        paging = header.get("paging", {})
        try:
            total = int(paging.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0

        body = result.get("body", {})
        rows_block = body.get("rows") or []
        # rows 는 [{"row":[...]}] 형태(레거시) 또는 [{...}] 형태일 수 있어 둘 다 흡수.
        out: list[dict] = []
        for block in rows_block:
            if isinstance(block, dict) and "row" in block:
                inner = block["row"]
                out.extend(inner if isinstance(inner, list) else [inner])
            elif isinstance(block, dict):
                out.append(block)
        if not out and isinstance(body.get("row"), list):
            out = body["row"]

        elapsed = int((time.monotonic() - t0) * 1000)
        logger.info(
            "openDataApi opnSvcId=%s local=%s p%d -> %d행/%d (%dms)",
            opn_svc_id, local_code, page, len(out), total, elapsed,
        )
        return out, (total or len(out))

    @staticmethod
    def _normalize(row: dict) -> BusinessLicense:
        def g(*keys):
            # 레거시는 camelCase, 일부 노출은 UPPER. 둘 다 시도.
            for k in keys:
                if k in row and row[k] not in (None, ""):
                    return str(row[k]).strip()
            return None

        return BusinessLicense(
            mgt_no=g("mgtNo", "MGTNO"),
            opn_svc_id=g("opnSvcId", "OPNSVCID"),
            opn_svc_nm=g("opnSvcNm", "OPNSVCNM"),
            bplc_nm=g("bplcNm", "BPLCNM"),
            rdn_whl_addr=g("rdnWhlAddr", "RDNWHLADDR"),
            site_whl_addr=g("siteWhlAddr", "SITEWHLADDR"),
            trd_state_gbn=g("trdStateGbn", "TRDSTATEGBN"),
            trd_state_nm=g("trdStateNm", "TRDSTATENM"),
            apv_perm_ymd=g("apvPermYmd", "APVPERMYMD"),
            dcb_ymd=g("dcbYmd", "DCBYMD"),
            x=g("x", "X"),
            y=g("y", "Y"),
            last_mod_ts=g("lastModTs", "LASTMODTS"),
        )
