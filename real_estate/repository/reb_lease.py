"""REB 상업용부동산 임대동향 — 지역 임대시장 환경(공실률·임대료·수익률).

RebRoneClient로 한국부동산원 R-ONE 통계를 조회해, 특정 지역(서울/권역)의
상가유형별 공실률·임대료·수익률을 최신 분기 + 추세로 제공한다. 평가 대상
건물 자체 값이 아니라 임차인이 처한 **임대시장 환경(context)**이다 — REB는
시/권역 단위라 구·상권·건물 단위가 없다.

STATBL_ID 카탈로그(2024Q3~ 현행, DTACYCLE QY). 분기 시계열이 길어지면 REB가
새 표로 분리하므로 표 ID는 갱신될 수 있다(SttsApiTbl로 재조회).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("repository.reb_lease")

# metric → 상가유형 → STATBL_ID (임대동향, 2024년3분기~, 분기)
CATALOG: dict[str, dict[str, str]] = {
    "공실률": {
        "오피스": "TT244763134428698", "중대형상가": "T249633134845544",
        "소규모상가": "T241833134686576", "집합상가": "T243283134931290",
        "일반상가": "T262303140824764", "일반상가1층": "T268603140832693",
    },
    "임대료": {
        "오피스": "TT249843134237374", "중대형상가": "T244363134858603",
        "소규모상가": "T248223134698125", "집합상가": "T244913134948657",
    },
    "수익률": {
        "오피스": "T245883135037859", "중대형상가": "T242083134887473",
        "소규모상가": "T246253134913401", "집합상가": "T246393134978815",
    },
}


class RebLeaseRepository:
    """REB 임대동향 조회 — 지역·상가유형별 공실률/임대료/수익률 + 추세."""

    def __init__(self, client):
        self._c = client

    def market(self, metric: str, building_type: str, *, region: str = "서울",
               recent: int = 4) -> dict | None:
        """한 지역·상가유형·지표의 최신 분기값 + 최근 추세.

        Args:
            metric: "공실률" | "임대료" | "수익률"
            building_type: "중대형상가" | "소규모상가" | "오피스" | "집합상가" ...
            region: REB 분류명 (예: "서울", "서울>기타", "서울>강남").
            recent: 추세 계산에 쓸 최근 분기 수.

        Returns:
            {metric, building_type, region, 기준분기, 값, 단위, 추세_pct, 시계열}
            데이터 없으면 None.
        """
        statbl = CATALOG.get(metric, {}).get(building_type)
        if not statbl:
            return None
        rows = self._c.get_data(statbl, "QY")
        series = []
        for r in rows:
            reg = r.get("CLS_FULLNM") or r.get("CLS_NM") or ""
            val = r.get("DTA_VAL")
            if reg == region and val not in (None, "", "-"):
                try:
                    series.append((r["WRTTIME_IDTFR_ID"], float(val),
                                   r.get("UI_NM") or "", r.get("WRTTIME_DESC") or ""))
                except (ValueError, TypeError):
                    continue
        if not series:
            return None
        series.sort(key=lambda x: x[0])
        window = series[-recent:]
        latest_q, latest_v, unit, desc = window[-1]
        first_v = window[0][1]
        trend = round((latest_v - first_v) / first_v * 100, 1) if first_v else None
        return {
            "metric": metric, "building_type": building_type, "region": region,
            "기준분기": latest_q, "기준분기설명": desc, "값": latest_v, "단위": unit,
            "추세_pct": trend, "추세_시작": window[0][0],
            "시계열": [(q, v) for q, v, _, _ in window],
        }
