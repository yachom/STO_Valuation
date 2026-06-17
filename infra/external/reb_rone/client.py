"""한국부동산원 R-ONE Open API HTTP 클라이언트 (urllib).

스펙(오픈API명세서) 기반:
    BASE = https://www.reb.or.kr/r-one/openapi
    기본인자: Key(필수), Type(xml|json), pIndex, pSize(≤1000)
    응답: {"<Op>": [{"head":[{"list_total_count":N},{"RESULT":{"CODE":"INFO-000"}}]},
                    {"row":[ {...}, ... ]}]}
    RESULT.CODE: INFO-000 정상 / INFO-200 데이터없음 / ERROR-290 키오류 /
                 ERROR-336 1000건초과 / ERROR-337 일일트래픽초과 ...
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from infra.gateway import APIGateway, get_default

logger = logging.getLogger("external.reb_rone")

_MAX_PSIZE = 1000


class RebRoneError(RuntimeError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class RebRoneClient:
    """R-ONE 통계 API thin wrapper: 인증 + 페이징 + RESULT.CODE 처리."""

    API_NAME = "reb_rone"
    BASE = "https://www.reb.or.kr/r-one/openapi"

    def __init__(self, key: str, *, timeout: float = 20.0,
                 gateway: APIGateway | None = None):
        self.key = key
        self.timeout = timeout
        self._gateway = gateway if gateway is not None else get_default()

    # ── 공개 API ──────────────────────────────────────────────────
    def list_tables(self, *, statbl_id: str | None = None,
                    max_rows: int = 1000) -> list[dict]:
        """통계표 목록 (SttsApiTbl). statbl_id 주면 해당 표만."""
        params = {"STATBL_ID": statbl_id} if statbl_id else {}
        return self._paged("SttsApiTbl", params, max_rows=max_rows)

    def list_items(self, statbl_id: str, *, itm_tag: str | None = None) -> list[dict]:
        """통계표 세부항목 (SttsApiTblItm) — ITM_ID/ITM_NM(공실률 등)."""
        params = {"STATBL_ID": statbl_id}
        if itm_tag:
            params["ITM_TAG"] = itm_tag
        return self._paged("SttsApiTblItm", params)

    def get_data(self, statbl_id: str, dtacycle_cd: str = "QY", *,
                 cls_id: str | None = None, itm_id: str | None = None,
                 wrttime: str | None = None, start: str | None = None,
                 end: str | None = None, max_rows: int = 4000) -> list[dict]:
        """통계 조회 (SttsApiTblData). 서버측 필터: cls_id(지역)·itm_id(항목)·기간.

        반환 row 필드: CLS_NM/CLS_FULLNM(지역 위계), ITM_NM, DTA_VAL, UI_NM,
        WRTTIME_IDTFR_ID(예 202502), WRTTIME_DESC.
        """
        params: dict = {"STATBL_ID": statbl_id, "DTACYCLE_CD": dtacycle_cd}
        if cls_id:
            params["CLS_ID"] = cls_id
        if itm_id:
            params["ITM_ID"] = itm_id
        if wrttime:
            params["WRTTIME_IDTFR_ID"] = wrttime
        if start:
            params["START_WRTTIME"] = start
        if end:
            params["END_WRTTIME"] = end
        return self._paged("SttsApiTblData", params, max_rows=max_rows)

    # ── 내부 ──────────────────────────────────────────────────────
    def _paged(self, op: str, params: dict, *, max_rows: int = 1000) -> list[dict]:
        rows: list[dict] = []
        page = 1
        while len(rows) < max_rows:
            size = min(_MAX_PSIZE, max_rows - len(rows))
            batch, total = self._call(op, params, page, size)
            rows.extend(batch)
            if not batch or len(rows) >= total:
                break
            page += 1
        return rows

    def _call(self, op: str, params: dict, page: int, size: int) -> tuple[list, int]:
        q = {"KEY": self.key, "Type": "json", "pIndex": page, "pSize": size, **params}
        # 엔드포인트는 .do 접미사 필요 (명세서엔 생략돼 있으나 실제 호출은 .do).
        url = f"{self.BASE}/{op}.do?{urllib.parse.urlencode(q)}"
        self._gateway.wait(self.API_NAME)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RebRoneError(f"REB HTTP {exc.code}", code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise RebRoneError(f"네트워크 오류: {exc.reason}") from exc

        data = json.loads(raw)
        blocks = data.get(op)
        if not isinstance(blocks, list):
            # 에러는 종종 {"RESULT":{"CODE":...,"MESSAGE":...}} 형태로도 옴
            res = (data.get("RESULT") or {})
            raise RebRoneError(f"REB 응답 이상: {res.get('MESSAGE') or raw[:200]}",
                               code=res.get("CODE"))
        head = {k: v for b in blocks if "head" in b for d in b["head"] for k, v in d.items()}
        result = head.get("RESULT", {})
        code = result.get("CODE", "")
        if code and not code.startswith("INFO-0"):
            if code == "INFO-200":          # 데이터 없음 — 빈 결과로 처리
                return [], 0
            raise RebRoneError(f"REB {code}: {result.get('MESSAGE')}", code=code)
        rows = [r for b in blocks if "row" in b for r in b["row"]]
        total = int(head.get("list_total_count") or len(rows))
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.info("%s p%d/%d -> %d행/%d (%dms)", op, page, size, len(rows), total, elapsed)
        return rows, total
