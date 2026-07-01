# 임차(tenant) 데이터 소스 — LocalData 인허가 / REB 임대동향

상가 부동산 평가의 **임차 축(tenant)** grounding을 위한 외부 데이터 API 조사·연동 기록.
공실률·임대료(시장환경)와 임차 회전율(폐업이력)을 어디서 어떻게 얻는지 정리한다.

> 요약: **상가형 테넌트 회전율 = LocalData 인허가**, **임대시장 환경 = REB 임대동향**,
> **오피스 입주사 = 뉴스/DART**(인허가에 없음). 통합 단일 API는 없다(소상공인 상가정보는 미사용 결정).

---

## 1. LocalData 지방행정 인허가 — 임차 회전율(turnover)

행정안전부가 전국 자치단체 인허가 데이터를 **공공데이터포털(data.go.kr)로 통합**(2026-04-16
localdata.go.kr 포털 폐쇄). **업종별 209종 개별 API** — 통합 단일 API는 없음.

- **org/엔드포인트**: `https://apis.data.go.kr/1741000/{service}/info` (현황, 영업+폐업 모두)
  - `/history`도 있으나 본 용도엔 `/info`가 폐업이력 포함(실측).
- **인증**: `DATA_GO_KR_API_KEY` (data.go.kr 계정 serviceKey, 데이터셋별 활용신청 필요).
  - 신청 직후 ~수 분 전파 지연 가능(403 Forbidden → 활성화 대기).
- **주요 파라미터**:
  - `serviceKey`, `pageNo`, `numOfRows`(≤100/page 권장), `returnType=json`
  - **`cond[ROAD_NM_ADDR::LIKE]=강서로 385`** ← 도로명주소 직접 필터 (건물 단위 조회의 핵심)
  - `cond[SALS_STTS_CD::EQ]` 영업상태, `cond[LCPMT_YMD::GTE]`/`[::LT]` 인허가일자 범위
- **응답 envelope**: 표준 `response.body.items.item[]`, `response.body.totalCount`.

### 응답 필드 (인허가 1건)
| 필드 | 의미 |
|---|---|
| `BPLC_NM` | 사업장명(상호) |
| `BZSTAT_SE_NM` / `SNTTN_BZSTAT_NM` | 업태(한식·일식 등) |
| `SALS_STTS_NM` / `DTL_SALS_STTS_NM` | 영업상태(영업/정상, 폐업) |
| `LCPMT_YMD` | 인허가일자(진입 시점) |
| `CLSBIZ_YMD` | **폐업일자**(퇴출 시점) — 회전율 핵심 |
| `ROAD_NM_ADDR` | 도로명주소 (+ 층/호수 포함) |
| `LOTNO_ADDR` | 지번주소 (+ 건물명) — bd_mgt_sn 매칭 |
| `LCTN_AREA` / `FCLT_TOTAL_SCL` | 점포 면적(㎡) |
| `CRD_INFO_X` / `CRD_INFO_Y` | 좌표(TM 중부, EPSG:5174 → WGS84 변환 필요) |
| `MNG_NO` | 관리번호(dedup 키) |
| `OPN_ATMY_GRP_CD` | 개방 자치단체(강서구=3150000) |
| `DAT_UPDT_PNT` | 데이터 갱신시점 |

### 신청 대상 데이터셋 (행안부 1741000, 같은 키로 전부 작동)
건물 테넌트 믹스에 맞는 **소수 핵심 업종만** 신청하면 됨(마곡=음식+의료가 대부분).

| 분류 | 데이터셋 | data.go.kr ID | 서비스 path |
|---|---|---|---|
| 외식 | 일반음식점 ✅ | 15154916 | `general_restaurants` |
| | 휴게음식점(카페·분식) | 15154921 | `rest_cafes` |
| | 제과점영업 | 15155252 | (신청 후 확인) |
| 의료 | 의원 | 15154874 | (신청 후 확인) |
| | 병원 | 15154458 | (신청 후 확인) |
| | 약국 | 15154822 | (신청 후 확인) |
| | 안경업 | 15154899 | (신청 후 확인) |
| 생활/소매 | 미용업 | 15154918 | (신청 후 확인) |
| | 통신판매업 | 15154963 | (신청 후 확인) |
| | 담배소매업 | 15155031 | (신청 후 확인) |

> 서비스 path는 각 데이터셋 openapi.do 페이지의 "Base URL"에서 확인 →
> `real_estate/repository/building_turnover.py:SERVICE_CATALOG`에 추가하면 자동 합산.

### 회전율 산출 (구현됨)
`real_estate/external/localdata/client.py`(LocalDataClient) + `real_estate/repository/building_turnover.py`.
도로명 → 누적폐업률·평균/중앙 존속연수·최근 회전·신규진입·업태분포. 미신청 업종은 403 graceful skip.

**검증 (강서로 385, 일반음식점):** 입점이력 44 / 영업 13 / 폐업 31 / 누적폐업률 70.5% /
평균존속 3.6년 / 영업중 평균영업 4.4년.

### ⚠️ 오피스는 없음
인허가 = 영업 인허가 업소(음식·의료·소매·서비스)만. **기업·법무·일반 사무실은 인허가 대상이
아니라 어떤 인허가 API에도 없음.** 오피스 입주사는 뉴스 마이닝(harvester) + DART로만 →
② 오피스 라우팅(navigator "{건물명} 입주 기업")으로 처리.

---

## 2. REB 상업용부동산 임대동향 — 임대시장 환경

한국부동산원 R-ONE. 공실률·임대료·수익률을 **지역(서울/권역) 단위**로 제공(건물·구 단위 아님)
→ 임차인이 처한 **시장환경 context**(이 건물 값이 아님).

- **엔드포인트**: `https://www.reb.or.kr/r-one/openapi/SttsApiTblData.do` (**.do 필수**)
  - 목록: `SttsApiTbl.do` / 항목: `SttsApiTblItm.do`
- **인증**: `REB_LEASE_MARKET_KEY` (KEY 파라미터). data.go.kr 아님(R-ONE 직접).
- **파라미터**: `KEY`, `STATBL_ID`, `DTACYCLE_CD=QY`, `Type=json`, `pIndex`, `pSize`(≤1000),
  `CLS_ID`(지역)·`ITM_ID`(항목)·`START_WRTTIME`/`END_WRTTIME`(기간) 서버측 필터.
- **응답**: `SttsApiTblData[].row[]` (`DTA_VAL`, `CLS_FULLNM` 지역위계, `WRTTIME_IDTFR_ID`),
  `head[].RESULT.CODE`(INFO-000=정상).
- **STATBL_ID 카탈로그**: `real_estate/repository/reb_lease.py:CATALOG` (공실률/임대료/수익률 ×
  오피스/중대형/소규모/집합, 2024Q3~ 현행).
- **지역 한계**: 서울 / 서울>도심·강남·영등포신촌·**기타**(강서 포함) 까지. 강서구·마곡 상권 단위 없음.

구현: `real_estate/external/reb_rone/`(client) + `repository/reb_lease.py`. tenant 축에 `MarketContext`
노드로 배선 완료(루브릭이 "서울 소규모상가 공실 +24% 악화" 인용).

---

## 3. 통합 상태
| 데이터 | 소스 | 구현 | tenant 축 연결 |
|---|---|---|---|
| 임대시장 환경(공실/임대료) | REB 임대동향 | ✅ client+repo | ✅ MarketContext 노드 |
| 임차 회전율(폐업이력) | LocalData 인허가 | ✅ client+repo+`BuildingTurnoverTool` | ✅ TurnoverStat 노드 (tenant_collector 배선, `HAS_TURNOVER`) |
| 오피스 입주사 | 뉴스/DART | harvester + 오피스 라우팅 | ✅ (② navigator purpose) |
| 입점업체 로스터 | 카카오 Local+스크래핑 | ✅ | ✅ Tenant 노드 |
