"""한국부동산원 R-ONE 부동산통계 Open API 어댑터.

상업용부동산 임대동향(공실률·임대료·수익률 등)을 분기 시계열로 제공한다.
data.go.kr 표준(apis.data.go.kr) envelope과 달라 PublicDataPortalClient 재사용
불가 — R-ONE은 SttsApiTblData[].row[] + RESULT.CODE 구조라 전용 어댑터를 둔다.

3개 엔드포인트:
    SttsApiTbl     통계표 목록 (STATBL_ID 검색)
    SttsApiTblItm  통계표 세부항목 (ITM_ID — 공실률/임대료 등)
    SttsApiTblData 통계 조회 (CLS_ID 지역·ITM_ID 항목·기간 필터)

로거: external.reb_rone.
"""

from infra.external.reb_rone.client import RebRoneClient, RebRoneError

__all__ = ["RebRoneClient", "RebRoneError"]
