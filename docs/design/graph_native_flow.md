# 그래프 네이티브 에이전트 흐름 설계 (graphflow)

> 브랜치: `yachom/feature/graph-native-flow`
> 상태: 상세 설계 v2 (2026-06-11, 토의 반영)
> 전제: 기존 에이전트 스택(`real_estate/report/` 전체)은 이 브랜치에서 **삭제됨**(커밋 b7a9e54).
> 데이터 축(location/asset/database/external/repository + infra/)만 공유.
> 삭제 전 v2/v3 작업분은 `yachom/wip/v3-pre-graphflow-snapshot`에 보존 — 복원 출처.

---

## 0. 결정 로그 (토의 확정 사항)

| # | 결정 | 내용 |
|---|---|---|
| D1 | 보고서 성격 | **정성 분석 보고서.** 정량 가치산정(수익환원 등) 없음. 가치에 영향 주는 요인의 근거 기반 서술 |
| D2 | 목차 | **기존 3축 유지** — tenant / esg / commercial + overview |
| D3 | Fact 입자 | **문장 단위 Fact 노드** + MENTIONS 엣지로 기존 노드 연결 |
| D4 | 순회 구조 | **walk 엔진 1개 + 교체 가능한 policy** — 보고서=StaticPolicy(가중치 표), QA·확장=LLMPolicy(홉당 배치 판정, 적응적 깊이). MAX 홉은 엔진 하드캡 |
| D5 | DB 구조 | **물리 DB 1개, 논리 2층** — 전역 층(자산 간 공유) + 자산 스코프 층(Asset 앵커 뷰). 자산 간 수집물 재사용 |
| D6 | QA 앵커 | 벡터 인덱스(card_text) + LLM 엔티티 힌트 추출 결합. 앵커 후보는 자산 스코프로 제한 |
| D7 | QA 라이브 확장 | **자동 발동.** 증거 부족 시 미니 EXPAND(1라운드, 검색 ≤3건) 실행 후 재탐색. "검색해도 없음"만 진짜 GAP |
| D8 | 확장 소스 | 네이버 검색+뉴스 스크랩(주력) + 공공 API 재조회(건축HUB·MOLIT 등). 웹 일반 검색(구글 등)은 추후 별도 브랜치 |
| D9 | 엔티티 연결 | 2단계 — 코드/주소 근거 있으면 확정(MENTIONS), 이름 유사만이면 별도 Entity + SAME_AS_CANDIDATE(미확정, Curator 승격) |
| D10 | 유효성/신선도 | **유효성 2단 모델** — Fact 추출 시 원문의 명시 유효기간(valid_until 등)을 속성으로 추출, 있으면 우선 적용. 없으면 반감기 감쇠 폴백. 공용 ValidityCheck 모듈을 보고서·QA·Auditor·Navigator가 공유 |
| D11 | QA 탐색 범위 | QA 초기 탐색(스코프 요약·앵커 매칭)은 **자산 N홉 스코프로 한정**. 스코프 밖 전역 검색은 하지 않음 — 부족하면 라이브 확장(D7)으로 새 증거를 스코프에 편입. 자산과 무관한 답변 구조적 차단 |
| D12 | 보고서 구조화 | 보고서의 1급 표현은 **주소 가능한 구조화 JSON**(section/block/claim ID). markdown은 렌더 산출물. 발행자 피드백은 본문 텍스트가 아니라 **대상 ID + 코멘트**로 입력 |
| D13 | 발행자 수정 루프 | Feedback 노드(TARGETS 엣지) + Reviser 에이전트. 피드백 종류별(edit/challenge/enrich) 타깃 한정 수정 — 같은 walk·미니 EXPAND 재사용, Auditor 재검사 후 새 report_version |
| D14 | 합의 기반 반영 + 첨부 자료 | 에이전트는 피드백·첨부 자료를 **맹신하지 않는다.** 그래프 증거와 대조한 Assessment(동의/반박/판단불가 + 인용 근거)를 회신, 다중 라운드 토의 후 **양측 합의(agreed) 시에만** 보고서 반영. 첨부 자료(PDF·의뢰자 문서)는 UserDoc으로 인입돼 일반 증거와 동일한 추출·검증 파이프라인 통과 |
| D15 | 그래프 정비 (Janitor) | 분열(동일 실체 복수 노드)·오병합(복수 실체 단일 노드)을 주기 배치로 점검. 자동 병합은 코드 수준 근거 시에만, 나머지는 리뷰 큐. 보고서 파이프라인과 독립된 배치 잡 |
| D16 | 자동화 수집 트랙 | 온디맨드(평가 시 EXPAND) 외에 스케줄 수집(Airflow 등)으로 자산 무관 최근 동향을 전역 층에 선적재 — LLM 지식 한계 보완. 수집 대상은 그래프가 결정(허브 엔티티·만료 임박 노드). 주기/소스 설계는 **별도 브랜치** |
| D17 | 담론 주도 큐레이션 (STORM/Co-STORM) | 고정 EXPAND(navigator→harvester 1패스)를 **관점 발견→시뮬레이션 Q&A→모더레이터→아웃라인** 담론 루프로 고도화. 그래프=Co-STORM 동적 마인드맵 재사용. 비결정적이므로 **config variant/flag 뒤** — 프로덕션은 기존 결정 경로(D4). 상세: `agentic_curation.md` |
| D18 | 관점 = 3축 척추 + 추가 발견 (보충) | tenant/esg/commercial 3축은 **구조화 데이터에 1:1 매핑된 고정 척추**로 유지(버리지 않음 — 데이터 소스 한계). Perspectivist는 그 위에 **추가 관점을 발견·허용**(additive, 상한 N) — 부모 축에 붙여 심화하거나 '특이사항' 섹션으로. 발견 관점은 **검색 기반으로만** 채움(D22). 3축 대체가 아니라 얹기 |
| D19 | 모더레이터 = 커버리지 기반 unknown-unknowns | auditor(정합성·모순)와 **역할 분리**. moderator는 **그래프 커버리지 메트릭**(관점별 Fact 밀도·미인용 노드·신선도 공백)으로 빈 곳에 질문 주입·종료 판정. D7(QA 자가확장)의 일반화 — 오래 미뤄둔 GAP→미니EXPAND 재진입을 원리적으로 배선 |
| D20 | 재현성 보존 | 담론 루프는 `curation.mode: static\|discourse` 플래그(기본 static). 프로덕션 의뢰 보고서는 StaticPolicy 결정 경로 유지(D4 일관), 담론은 A/B variant로만. PR마다 flag 격리 → 기존 경로 항상 작동 |
| D21 | writer 깊이 = 토큰 아닌 구조 캡 | writer가 max_tokens(8000)의 절반(~4192)만 쓰고 잘리지 않음 → 병목은 토큰이 아니라 프롬프트/스키마 구조 캡. 캡 완화(문단 3~5→4~7, 문장 3~6→4~8) + "깊이 있게(원인·함의·비교)" 지시. **단 grounding 불변** — 분량 위해 추측 금지, 모든 사실은 인용 evidence 범위 안 |
| D22 | 완전 자율의 도구 한계 → 검색 기반 보충 | 자율 구동 가능한 데이터 획득은 **검색(naver/web)뿐**. 구조화 소스(building_register·tenants·turnover·commercial_sales·reb_lease)는 seed/tenant 고정 배선 호출이고 LLM-callable 도구 아님(bind_tools 미사용). 자산당 구조화 데이터는 유한·고정이라 '도구 선택' 여지 작음 → **3축=구조화 척추(결정적) + 검색 기반 발견 관점(에이전틱·보충, D18)**. 완전 tool-agent(repository→BaseTool+bind_tools)는 ROI 낮아 보류 |

---

## 1. 목표

**입력**: 주소 1줄.
**출력**: STO 기초자산(상가 부동산)에 대한 3축 정성 분석 보고서 + 동일 그래프 기반 QA.

핵심 요구사항 2가지:

1. **QA = 보고서 생성과 동일한 과정.** 확장 데이터를 그래프 DB(Neo4j)에 저장하고,
   보고서 생성도 QA도 같은 노드/엣지 순회로 진행한다. QA에서 증거가 부족하면
   보고서 생성에 쓴 것과 **같은 확장 도구**(Navigator/Harvester)를 예산만 줄여
   자동 실행한다 (D7). 보고서와 QA는 "같은 그래프 + 같은 walk + 같은 확장 도구,
   예산과 정책만 다른" 시스템이다.

2. **멀티 홉 그래프 기반 데이터 확장.** 검색어를 LLM이 허공에서 짓지 않고,
   자산 노드에서 출발한 경로(중요 속성·연결 엔티티)에서 유도한다. 검색 결과는
   Fact/WebDoc 노드로 재적재되어 다음 홉의 출발점이 된다.

---

## 2. 데이터 기준선 (공유 레이어)

### 2.1 입력 처리

```
주소 문자열
  → LocationResolver (real_estate/location.py) : 카카오 주소검색 + 좌표→행정구역, Juso API
  → LocationInfo : 좌표, sigungu, hcode/bcode, bd_mgt_sn(건물관리번호 25자리), bun/ji
```

### 2.2 SQLite 진실 저장소 (`real_estate/database/DB/real_estate.db`)

MOLIT 실거래(아파트 매매/전월세, 상가 매매), BEEC/G-SEED 인증, 서울 상권
(유동/거주인구·추정매출, 행정동 폴백), REB 임대동향, NTS 사업자통계, KB 시세,
정비사업, 지하철/버스. **원본 raw 시계열은 계속 SQLite에 잔존** — 그래프엔
엔티티 + 관계 + 최신 분기 요약 프로퍼티만 올린다.

### 2.3 외부 클라이언트

`infra/external/` (naver_search, naver_news_scraper, public_data_portal,
seoul_open_data) + `infra/gateway` (토큰버킷/백오프) + `real_estate/external/`
(molit_rtms, building_hub).

### 2.4 스냅샷에서 선별 복원할 것

Neo4j driver/schema/cards, 임베더(bge-m3→e5), 벡터 인덱스, llm.py(get_llm),
트레이싱, report 저장 스키마, scout SQL 12종(Seeder가 흡수). 출처:
`yachom/wip/v3-pre-graphflow-snapshot`.

---

## 3. 핵심 설계 원칙

### 원칙 1 — 그래프 = 단일 작업 메모리, 논리 2층 구조 (D5)

물리 Neo4j는 1개. 그 안에 두 층위가 공존한다:

- **전역 층**: SQLite에서 적재된 구조 데이터(행정동, 상권, 단지, 역, 인증,
  거래) + 역대 평가·QA에서 수집된 WebDoc/Fact. **모든 자산이 공유.**
- **자산 스코프 층**: 평가 요청 시 `Asset` 노드를 만들고 전역 층의 관련 노드에
  `ANCHORS` 엣지로 연결. 자산용 그래프를 "생성"하는 게 아니라 **전역 그래프에
  깃발을 꽂고 깃발에서 닿는 범위를 뷰로 보는 것.**

이 구조의 핵심 가치: 자산 A 평가 중 수집한 "강남대로 상권 공실률 상승" Fact는
같은 상권의 자산 B 평가에서 그대로 재사용된다. 평가/QA가 쌓일수록 전역 그래프가
좋아지는 누적 효과. 수집물에는 `collected_by`(report_id/question_id) 프로퍼티로
출처를 남긴다.

에이전트의 모든 산출물(수집 사실, 추출 엔티티, 보고서 주장)은 노드/엣지로
적재되고, 모든 입력은 그래프 순회로 얻는다. LangGraph state에는 포인터만 남는다.

### 원칙 2 — 단일 walk 엔진 + 교체 가능한 policy (D4)

확장(어디를 더 팔까), 집필(이 섹션의 근거는), QA(이 질문의 근거는)는 모두
"앵커에서 출발해 의미 있는 경로로 모은 증거는 무엇인가"라는 같은 질문이다.
탐색 엔진은 하나로 고정하고, **"이 노드를 더 확장할까?" 판단부(policy)만
플러그인**으로 교체한다:

| | StaticPolicy | LLMPolicy |
|---|---|---|
| 판단 근거 | 엣지 **종류**별 가중치 표 | 노드 **내용**(card_text) vs 목적의 관계를 LLM이 판정 |
| 깊이 | 가중치 곱의 자연 감쇠로 결정 | 적응적 — frontier가 비면 조기 종료, MAX 홉 캡 |
| 재현성 | 같은 그래프 스냅샷 → 항상 같은 증거 | 비결정적 (판정 이유는 로그로 남음) |
| LLM 비용 | 0 | 홉당 1회 배치 판정 (질문당 ≤3회) |
| 사용처 | **보고서 섹션 집필** (Composer) | **QA 증거 탐색 + EXPAND frontier 선정** (Navigator) |

보고서 재현성의 정의: "같은 그래프 스냅샷 + 같은 정책 → 같은 증거". LLM은
temperature 0이어도 완전 결정적이지 않으므로 보고서 경로에서 배제한다.

### 원칙 3 — 경로가 검색어를 만든다 (path-grounded query expansion)

```
(Building "OO타워")-[:LOCATED_IN]->(AdminDong "역삼1동")
                                    <-[:SERVES_DISTRICT]-(CommercialArea "강남대로 상권")
  → "강남대로 상권 공실률 2026", "역삼동 상가 임대 시세"

(CommercialArea)-[:TOP_INDUSTRY]->(Industry "커피전문점")     # 2-hop
  → "강남대로 커피 프랜차이즈 출점", "역삼동 카페 폐업"
```

홉이 깊어질수록 검색어가 자산의 구체 맥락(상권명, 주력 업종, 인증 등급,
정비사업명)을 품는다. 모든 검색은 `(:Query)` 노드로 기록 — 어떤 경로가 이
검색을 낳았는지 provenance + 중복 검색 차단.

### 원칙 4 — 모든 주장은 그래프에 닿아야 한다 (claim grounding)

보고서의 주장(Claim)은 노드로 적재되고 `CITES` 엣지로 증거 노드에 연결된다.
**사실 주장(factual)은 그래프 인용 필수, 해석/의견(opinion)은 자기 보고서의
factual claim에서 도출됐음을 명시** (§10). Auditor 기계층이 "CITES 없는
factual Claim"을 LLM 없이 잡아낸다.

### 원칙 5 — 증거에는 유효기간이 있다 (D10)

원문에 명시된 유효기간("2027년 12월 준공 예정", "이달 말까지 모집")이 있으면
Fact의 속성(valid_until)으로 추출해 **그것을 우선 적용**하고, 없을 때만 반감기
감쇠로 폴백한다. "이 소스가 지금도 유효한가"의 판단은 공용 ValidityCheck 모듈
하나로 — 보고서 집필(Composer), QA, Auditor, Navigator가 전부 같은 로직을
쓴다 (§12). 오래된/만료 Fact는 삭제하지 않고 후순위로 밀리며 재검색 신호로
기능한다.

---

## 4. 그래프 스키마

기존 `schema.py`(스냅샷에서 복원)의 라벨/엣지에 추가한다.

### 4.1 기존 라벨 (전역 층 — SQLite 적재)

`Complex`(master_complex_id), `Building`(bdmgt_sn), `LegalDong`(bcode),
`AdminDong`(adstrd_cd), `CommercialArea`(trdar_cd), `DevelopmentProject`,
`Certification`(BEEC/GSEED 통합), `TenancyEvent`(MOLIT 통합),
`TransitStation`, `Report`, `ReportSection`, `Claim`.

### 4.2 신규 노드 라벨

| 라벨 | 키 | 생성 주체 | 주요 프로퍼티 |
|---|---|---|---|
| `Asset` | asset_id (`re:{bd_mgt_sn}`) | Seeder | 모든 walk의 기본 앵커. 주소, 좌표, building_name |
| `Industry` | industry_cd | Seeder | 상권 업종 (commercial_estimated_sales의 svc_induty_cd). 최신 분기 매출 요약 |
| `WebDoc` | url_hash | Harvester | title, url, published_at, source_tier(§9), fetched_at, collected_by |
| `UserDoc` | doc_hash | Reviser(첨부 인입) | 발행자/의뢰자 제공 자료 (PDF·문서). provider, received_at, source_tier=provisional(교차 검증 전, §10.4) |
| `Fact` | fact_id | Harvester | **원자적 사실 1문장** (D3). text, confidence(§9), published_at, event_date, valid_until·temporal_kind(§12), collected_by |
| `Entity` | entity_id | Harvester | 기존 라벨 매칭 실패한 추출 엔티티 (프랜차이즈, 정책, 인물 등). resolution_status |
| `Query` | query_hash | Navigator | text, executed_at, hop_path(낳은 경로), n_results |
| `Question` | question_id | QA | text, asked_at, answered(bool), expanded(bool, 라이브 확장 발동 여부) |

### 4.3 신규 엣지 타입

| 엣지 | 방향 | 설명 |
|---|---|---|
| `ANCHORS` | Asset → Building/AdminDong/CommercialArea/LegalDong | 자산의 구조적 위치 (Seeder) |
| `TOP_INDUSTRY` | CommercialArea → Industry | 상권 매출 상위 N 업종 |
| `SPAWNED_QUERY` | 노드 일반 → Query | 이 노드(경로 끝)가 낳은 검색 |
| `RETRIEVED` | Query → WebDoc | 검색 결과 |
| `EXTRACTED_FROM` | Fact → WebDoc | 추출 provenance |
| `MENTIONS` | Fact → 노드 일반 | **확정 연결** (코드/주소 근거, §9) |
| `SAME_AS_CANDIDATE` | Entity → 노드 일반 | **미확정 연결** (이름 유사만, walk에서 저가중치, §9) |
| `ABOUT` | WebDoc → Entity/노드 일반 | 문서 주제 |
| `ASKED` | Question → Asset | QA 이력 |
| `ANSWERED_BY` | Question → Claim/Fact | 답변 근거 |
| `GAP_AT` | Question → 노드 일반 | 라이브 확장까지 했는데도 못 답한 위치 (D7) |

기존 `HAS_SECTION`(Report→ReportSection), `HAS_CLAIM`(Section→Claim),
`CITES`(Claim→노드 일반)는 이번에 실제로 채운다.

### 4.4 자산 스코프 적재 (Seeder의 일)

전체 SQLite 일괄 적재(구 ingest_all) 대신 **자산 1건 기준 스코프 적재**:

- 본 건물: bd_mgt_sn 매칭 — 건축물대장(건축HUB 호출: 용도/연면적/층수/사용승인일),
  BEEC/G-SEED 인증, 상가 매매 실거래
- 위치: 법정동/행정동 노드 + ANCHORS
- 상권: 좌표 point-in-polygon 상권 + 인접 상권, TOP_INDUSTRY(매출 상위 5 업종),
  최신 분기 유동/거주인구·매출 요약 프로퍼티
- 배후수요: 반경 800m 아파트 단지 + 최근 거래 요약
- 인프라: 최근접 지하철역/버스, 자치구 정비사업
- 시장: REB 임대동향·NTS 사업자통계 최신 분기 요약 (시군구 단위 프로퍼티)

구 scout 12종의 SQL이 이 적재 쿼리로 흡수된다. 모든 신규 노드에 card_text
(한국어 1~3문장 요약, cards.py 템플릿 확장) 생성 + 벡터 인덱스 등록.
idempotent(MERGE) — 같은 자산 재평가 시 갱신만 일어난다.

---

## 5. GraphWalk 엔진 상세

### 5.1 엔진 인터페이스

```python
# graphflow/traversal.py
def walk(
    anchors: list[NodeRef],
    policy: WalkPolicy,            # StaticPolicy | LLMPolicy
    *,
    max_hops: int = 3,             # 엔진 하드캡 — policy가 요청해도 초과 불가
    k: int = 12,                   # 반환 경로 수
) -> list[EvidencePath]

@dataclass
class EvidencePath:
    nodes: list[NodeCard]          # card_text 체인
    edges: list[EdgeRef]           # 엣지 타입 + elementId
    score: float                   # §5.4
    text: str                      # LLM 컨텍스트 직렬화 (§5.5)
```

### 5.2 StaticPolicy — 가중치 표

가중치는 **엣지 종류에 붙는 통과 우선순위**다 (개별 노드가 아님). 표는 작다:
엣지 종류 ~14개 × 프로파일 4개.

```python
# graphflow/profiles.py — 초기값. 절대값보다 순서가 중요: 상(0.8~1.0)/중(0.4~0.7)/하(0.1~0.3)
PROFILES = {
    "tenant": {     # 임차/임대 가치
        ANCHORS: 1.0, HAS_TENANCY: 0.9, MENTIONS: 0.7, EXTRACTED_FROM: 0.5,
        LOCATED_IN: 0.5, WITHIN_RADIUS: 0.4, SERVES_DISTRICT: 0.3,
        TOP_INDUSTRY: 0.2, CERTIFIED_AS: 0.1, SAME_AS_CANDIDATE: 0.1,
    },
    "esg": {
        ANCHORS: 1.0, CERTIFIED_AS: 0.9, MENTIONS: 0.7, EXTRACTED_FROM: 0.5,
        LOCATED_IN: 0.3, SERVES_DISTRICT: 0.2, TOP_INDUSTRY: 0.1,
        HAS_TENANCY: 0.1, SAME_AS_CANDIDATE: 0.1,
    },
    "commercial": {
        ANCHORS: 1.0, SERVES_DISTRICT: 0.9, TOP_INDUSTRY: 0.8, MENTIONS: 0.7,
        WITHIN_RADIUS: 0.6, EXTRACTED_FROM: 0.5, LOCATED_IN: 0.5,
        HAS_TENANCY: 0.2, CERTIFIED_AS: 0.1, SAME_AS_CANDIDATE: 0.1,
    },
    "qa_base": {    # QA에서 LLM boost의 베이스라인
        ANCHORS: 1.0, MENTIONS: 0.7, HAS_TENANCY: 0.6, SERVES_DISTRICT: 0.6,
        TOP_INDUSTRY: 0.6, CERTIFIED_AS: 0.6, WITHIN_RADIUS: 0.5,
        EXTRACTED_FROM: 0.5, LOCATED_IN: 0.4, SAME_AS_CANDIDATE: 0.1,
    },
}
```

곱셈 구조라 깊이 페널티가 내장된다 — 0.7 엣지 3홉 = 0.34로 자연 감쇠.

**운용 방법**: ① 초기값은 도메인 직관으로 (위 표). ② 대표 질문 20~30개
golden set을 만들어 walk 결과를 눈검사. ③ "esg 섹션에 상권 매출이 끼어든다"
같은 패턴이 보이면 해당 엣지를 한 단계(tier) 조정. ④ Auditor의 "증거 빈약"
판정과 QA 미스 로그가 튜닝 대상을 알려준다. 표는 코드라 git으로 이력 관리.

### 5.3 LLMPolicy — 홉당 배치 판정 (적응적 깊이)

사용자 제안 채택: 새 엔티티와 목적의 관계를 보고 더 갈지 판단. MAX 홉은
엔진 캡, 실제 깊이는 적응적.

```
frontier = anchors; hop = 0
while frontier and hop < max_hops:
    candidates = frontier의 이웃 노드들 (card_text, 최대 ~30개)
    verdicts = LLM 1회 호출:                       # 홉당 배치 판정 — 호출 수 = 홉 수
        "목적: {질문 or 섹션 주제}
         각 후보를 [채택(증거로 사용) / 확장(더 판다) / 버림] 판정 + 이유 1줄"
    채택 노드 → 증거 경로에 추가
    frontier = 확장 판정 노드                       # 비면 자연 조기 종료
    hop += 1
```

실행 예 — 질문 *"1층에 카페 입점 가능성 있어?"*:

```
hop 1: Cert(BEEC)→버림 "무관" / TenancyEvent→채택 "임대시세 근거"
       / CommercialArea→확장 "입점은 상권 수요 문제"
hop 2: Industry(커피전문점)→확장 "직결" / Fact"공실률 상승"→채택
hop 3: Fact"저가커피 출점 가속"→채택 / frontier 빔 → 종료
```

판정 이유가 전부 trace에 남는다 (왜 버렸는지 디버깅 가능). 비결정적이므로
보고서 집필에는 쓰지 않는다.

### 5.4 경로 점수

```
score(path) = ∏ 엣지가중치(LLMPolicy면 채택 확신도)
            × 유효성(끝 노드, §12 ValidityCheck — 보고서·QA 공용)
            × 신뢰도(끝 노드가 Fact면 confidence, §9)
```

같은 노드에 도달하는 경로가 여럿이면 **최고 점수 경로 1개만** 남긴다
(증거 중복 인용 방지).

### 5.5 증거 직렬화

LLM에 줄 때 경로를 카드 체인으로 편다:

```
[E3] 강남대로 상권 (골목상권, 면적 12,400㎡)
     —[TOP_INDUSTRY]→ 커피전문점 (분기매출 84억, 점포 31개)
     —[MENTIONS by]→ "저가커피 브랜드의 강남대로 출점이 가속되고 있다"
                      (2026-03 한국경제, confidence 0.8)
```

관계가 보이면 LLM이 "상권을 경유한 정보"임을 인지하고 서술에 반영한다 —
평면 스니펫 목록 대비 그래프의 실질 이득.

### 5.6 앵커 결정 (D6)

보고서 집필: 앵커 = `[Asset]` 고정.
QA: 2단 결합 —

1. **LLM 힌트 추출**: 프롬프트에 그래프 전체가 아니라 **자산 스코프 지도 요약**
   (라벨 종류 + 주요 엔티티 수십 개 이름 — 질문 시점에 그래프에서 조회하므로
   항상 최신)을 넣고, 질문에서 검색 힌트를 뽑는다:
   `"1층에 스타벅스 들어올까?" → ["커피전문점", "상권 공실", "임대료"]`
2. **벡터 매칭**: 힌트 + 질문 원문을 card_text 벡터 인덱스로 검색 → 실제 노드.
   후보는 **Asset에서 N홉 이내로 제한**(범위 누출 방지). Claim/ReportSection
   노드도 후보에 포함 (종합 질문은 보고서 결론에 매칭됨).

**초기 탐색은 자산 스코프가 전부다 (D11).** 프롬프트에 넣는 지도 요약도, 벡터
후보도 자산 N홉(기본 3) 안으로 한정하고, **스코프 밖 전역 그래프는 검색하지
않는다** — 전역 층의 다른 자산 수집물 중 이 자산과 관련 있는 것은 어차피 공유
노드(상권·행정동)를 통해 N홉 안에 들어와 있고, 그렇지 않은 것은 이 자산의
답변에 끼면 안 되는 노이즈다. 스코프 안에서 증거가 부족하면 전역을 뒤지는 게
아니라 라이브 확장(D7)으로 새 증거를 수집해 스코프에 편입시킨다.

**실패 모드와 완화**:

| 실패 | 증상 | 완화 |
|---|---|---|
| 표현 불일치 | 그래프에 없는 주제인데 차선 노드가 잡혀 그럴듯한 오답 | 유사도 임계 미달 시 "근거 없음" + 라이브 확장(D7) |
| 추상 질문 | "투자할 만해?" — 앵커 산발 | Claim/Section 노드를 앵커 후보에 포함 |
| 범위 누출 | "강남" 노드 수백 개 중 무관 노드 매칭 | 자산 스코프 N홉 제한 |
| 카드 빈약 | "법정동 1168010100." 수준이면 매칭 불가 | 신규 라벨 카드 템플릿 품질 관리 (§4.4) |

### 5.7 LLM 파라미터의 안전장치 (QA)

LLM이 만드는 것은 쿼리(코드)가 아니라 파라미터(데이터):

```python
plan = llm.invoke(..., structured=WalkPlan)
# {"entity_hints": [...], "profile": "commercial", "boost": {"MENTIONS": +0.2}, "hops": 2}
paths = walk(
    anchors=vector_match(plan.entity_hints),
    policy=StaticPolicy(PROFILES[plan.profile], boost=clamp(plan.boost, ±0.3)),
    max_hops=min(plan.hops, HARD_CAP),
)
```

JSON 스키마 검증 + boost 클램프 + hops 하드캡. LLM이 이상한 plan을 내도
"덜 적절한 증거"가 나올 뿐 시스템이 깨지지 않고, citation으로 사후 추적 가능.
(QA는 기본 LLMPolicy를 쓰되, plan 기반 StaticPolicy는 비용 절감 폴백 옵션.)

---

## 6. 에이전트 구성 (roster)

| # | 에이전트 | LLM | 역할 |
|---|---|---|---|
| 1 | **Seeder** | ❌ | 주소 → LocationResolver → Asset 앵커 + 자산 스코프 적재(§4.4) + card_text + 벡터 인덱스. 결정적, idempotent |
| 2 | **Navigator** | ✅ Sonnet | LLMPolicy walk로 frontier 수집 → 확장 타깃 선정(§8.1) + 경로 기반 검색어 생성(§8.2) → Query 노드 적재 |
| 3 | **Harvester** | ✅ Sonnet | Query 실행(네이버 검색/스크랩, 공공 API 재조회) → WebDoc 적재 → Fact/Entity 추출(§9) → MENTIONS/SAME_AS_CANDIDATE 연결 |
| 4 | **Curator** | ✅ Haiku | 엔티티 dedup, SAME_AS_CANDIDATE 승격 판정(§9.2), 신규 노드 card_text, 벡터 인덱스 갱신 |
| 5 | **Composer** | ❌ | 축별 StaticPolicy walk → EvidencePath k=12 직렬화. 순수 Cypher+점수 계산 |
| 6 | **Writer** ×3축 | ✅ Sonnet | 섹션 본문 + 문장-인용 쌍 structured output(§10.1) → Claim/CITES 적재 |
| 7 | **Synthesizer** | ✅ Opus | 3축 섹션 → Overview (요약 + 축간 인사이트 + 정성 종합의견). Claim grounding 동일 |
| 8 | **Auditor** | 반반 | 기계층(CITES/실존/수치, §10.2) + LLM층(모순/과대해석). fail → 재작성 or GAP → 미니 EXPAND |
| 9 | **QA Responder** | ✅ Sonnet | 앵커 결정(§5.6) → LLMPolicy walk → 충분성 판정 → (부족 시) 미니 EXPAND 자동 발동(§11) → 답변+인용 |
| 10 | **Reviser** | ✅ Sonnet | 발행자 피드백(대상 ID+코멘트, §10.4)을 kind별 처리 — 타깃 한정 재작성/재검증/보강. 수정분은 Auditor 기계층 재검사 후 새 버전 |

planner는 없다. 목차는 3축 고정(D2)이고, 각 축의 세부 논점은 walk가 모은
증거 분포에서 나온다.

---

## 7. 워크플로우 (LangGraph)

```
                ┌──────────────────────────────────────────┐
 주소 ─────────▶│ Phase 1: SEED                            │
                │ Seeder: Asset 앵커 + 스코프 적재 + 카드   │
                └──────────────┬───────────────────────────┘
                               ▼
                ┌──────────────────────────────────────────┐
                │ Phase 2: EXPAND (멀티 홉 루프, ≤3라운드)   │
                │  ┌→ Navigator: frontier 선정+검색어 생성  │
                │  │   Harvester: 검색→WebDoc→Fact/Entity  │
                │  │   Curator:   dedup+승격+카드+인덱스    │
                │  └─ novelty ≥ 0.3 & round < 3 ────────┐  │
                └──────────────┬────────────────────────┘  │
                               ▼ (novelty < 0.3 or 예산 소진)
                ┌──────────────────────────────────────────┐
                │ Phase 3: COMPOSE & WRITE (3축 병렬)       │
                │  Composer(StaticPolicy walk)             │
                │  → Writer(문장-인용 쌍 → Claim+CITES)     │
                │  → Synthesizer(Overview)                 │
                └──────────────┬───────────────────────────┘
                               ▼
                ┌──────────────────────────────────────────┐
                │ Phase 4: AUDIT (≤2회 재진입)              │
                │  기계층: CITES/실존/수치 → fail시 Writer   │
                │  LLM층: 모순/과대해석 → GAP 마킹           │
                │          → 미니 EXPAND → Phase 3 재진입   │
                └──────────────┬───────────────────────────┘
                               ▼
                  보고서 저장 (SQLite + 구조화 JSON + md 렌더, §10.3)
                  + Report 서브그래프 적재 (HAS_SECTION/HAS_CLAIM/CITES)
                               ▼
                ┌──────────────────────────────────────────┐
                │ Phase 5: REVISE (발행자 상호작용, 이벤트 구동)│
                │  발행자: 대상 ID + 코멘트 [+ 첨부 자료]     │
                │  → Feedback 노드 적재 (TARGETS 엣지)       │
                │  ┌→ Reviser: 그래프 증거 대조 평가          │
                │  │   → Assessment(동의/반박+인용) 회신      │
                │  └─ 발행자 재반박/자료 추가 (토의 루프 §10.5)│
                │  → 양측 합의(agreed) 시에만:               │
                │     kind별 타깃 한정 수정 (§10.4)          │
                │     → Auditor 기계층 → 새 report_version  │
                └──────────────────────────────────────────┘

 ─────────────── QA (별도 entry, 같은 그래프) ───────────────
 질문 → 앵커(§5.6) → LLMPolicy walk → 충분성 판정
   ├─ 충분 → 답변 + 인용(Claim/Fact)
   └─ 부족 → 미니 EXPAND 자동 발동 (1라운드, 검색 ≤3)      ← D7
              → 수집물 그래프 적재 → walk 재실행
                ├─ 확보 → 답변 ("신규 수집 근거" 라벨 표시)
                └─ 여전히 없음 → GAP_AT 마킹 + "범위 밖" 응답
 ※ GAP 누적분 + QA 수집물은 다음 보고서 라운드에 자동 반영
```

### state (포인터만)

```python
class GraphFlowState(TypedDict):
    asset_id: str
    report_id: str
    round: int            # EXPAND 라운드
    novelty: float        # 직전 라운드 신규성
    audit_iter: int       # AUDIT 재진입 카운터 (≤2)
    sections_done: list[str]
```

작업 메모리(증거, 개념, 주장)는 전부 그래프 — v3의 mind_map/evidence_pool 같은
대형 state dict가 사라진다.

---

## 8. EXPAND 루프 상세

### 8.1 frontier 선정 (Navigator)

```
frontier 점수 = 경로 가중치          # 자산과 의미적으로 가까운가
              × 1/(1+기존 증거 수)    # 이미 충분히 아는 노드는 후순위
              × 신선도 요구          # 시계열성 주제(공실·개발·업종)는 가산,
                                    #   오래된 Fact만 있는 노드도 가산(갱신 신호, §12)
```

예: "BEEC 1등급" 노드는 1홉이지만 정적 사실 + 증거 1건 충분 → 후순위.
"강남대로 상권"은 통계 2건뿐 + 시계열성 강함 → 최우선.

예산: 라운드 ≤3, 라운드당 frontier ≤8 노드, 노드당 검색어 ≤2, 총 검색 ≤40.

### 8.2 검색어 생성 — 2단

1. **경로 템플릿 전개** (결정적): `{상권명} {업종} 폐업`, `{행정동} {사업명} 진행`,
   `{건물명} 임대` — 경로 위 고유명사가 슬롯에 박힌다.
2. **LLM 다듬기**: 자연스러운 검색어로 변형하되 **경로 고유명사 포함을 강제**.
   → LLM이 자산과 무관한 일반론("상가 투자 전망")으로 새는 것을 ①이 차단.

dedup: Query.query_hash(정규화 텍스트) + 임베딩 유사도로 기실행 검색 회피.

### 8.3 검색 소스 (D8)

- **네이버 검색 + 뉴스 스크랩** (주력): naver_search(news/blog/webkr) →
  뉴스 호스트면 본문 스크랩. infra/gateway 경유 (토큰버킷/백오프).
- **공공 API 재조회**: 확장 중 발견된 식별자(인근 건물 주소, 단지명 등)에 대해
  건축HUB 대장·MOLIT 실거래를 추가 호출해 정형 노드로 적재. Harvester가
  "추출 엔티티에 주소/코드가 있으면 공공 API 우선" 규칙.
- 웹 일반 검색(구글 등): 추후 별도 브랜치 (새 클라이언트 필요).

### 8.4 종료 조건 — novelty

라운드 종료 시: 신규 Fact를 기존 Fact 임베딩과 비교, **유사도 < 0.85인 비율 =
novelty**. novelty < 0.3이면 조기 종료 — 더 파도 같은 얘기(기사 재탕)만 나온다는
뜻. 이 장치가 없으면 증거 빈약 자산에서 예산을 항상 소진한다.

### 8.5 미니 EXPAND (QA용 축소판, D7)

같은 Navigator/Harvester/Curator를 예산만 줄여 호출: **1라운드, frontier ≤2,
검색 ≤3, novelty 체크 생략.** 차이는 frontier 선정 기준이 "질문과의 관련"이라는
것뿐 (Navigator의 목적 입력이 섹션 주제 대신 질문).

---

## 9. Fact 추출과 엔티티 연결 (오염 방지)

### 9.1 추출

Harvester가 WebDoc 본문에서 **원자적 사실 1문장 단위**로 추출 (D3):

```json
{"text": "강남대로 상권의 1분기 중대형 상가 공실률이 전분기 대비 1.2%p 상승했다",
 "mentions": [{"surface": "강남대로 상권", "match_hint": {"trdar_name": "강남대로"}}],
 "published_at": "2026-04-02", "extraction_confidence": 0.9}
```

### 9.2 연결 — 2단계 (D9)

잘못된 연결은 walk를 타고 보고서/QA 전체로 번지므로 보수적으로:

- **확정 (`MENTIONS`)**: 코드 수준 근거가 있을 때만 — 주소 일치, 고유 코드 매칭,
  상권명+행정동 동시 일치. 기계 검증 가능.
- **미확정 (`SAME_AS_CANDIDATE`)**: 이름 유사도만 있으면 기존 노드에 붙이지 않고
  **별도 Entity 노드 신설** + 후보 엣지. walk는 이 엣지를 가중치 0.1로만 건넌다.
  Curator가 후속 라운드에서 추가 증거(주소 동시 언급 등) 확보 시 MENTIONS로
  승격하고 Entity를 병합.

### 9.3 confidence

```
Fact.confidence = source_tier × extraction_confidence
  source_tier: 공공기관/공시 1.0 > 주요 언론 0.8 > 일반 웹/블로그 0.4
```

**Writer 제약**: confidence < 0.5인 Fact를 단독 근거로 factual Claim 생성 금지
("~라는 미확인 언급이 있다" 수준으로만 서술 가능). Auditor 기계층이 검사.

---

## 10. 보고서 생성 로직

### 10.1 Writer — 문장-인용 쌍 강제

입력: Composer의 EvidencePath ~12개 (축별 StaticPolicy walk).
출력 (structured output):

```json
{"title": "상권 가치 분석",
 "blocks": [{"block_id": "blk_c1", "markdown": "...", "claim_ids": ["clm_17"]}],  // §10.3 구조와 1:1
 "claims": [
   {"text": "본 건물이 속한 강남대로 상권의 공실률은 2분기 연속 상승했다",
    "kind": "factual",
    "cites": ["fact:abc123", "reb:2026q1"],          // factual → 그래프 인용 필수
    "as_of": "2026-04"},                              // 날짜 명시 의무 (D10)
   {"text": "단기 임대수요 회복은 제한적일 것으로 판단된다",
    "kind": "opinion",
    "derived_from": ["claim:c1", "claim:c2"]}         // opinion → 자기 factual에서 도출 명시
 ]}
```

factual/opinion 구분으로 "근거 없는 의견"과 "근거에서 비약한 의견"을 분리해
잡는다. Claim은 노드로 적재되고 CITES 엣지가 생긴다. 보고서 산출 형식(SQLite
report/report_version + md sidecar)은 기존 그대로.

### 10.2 Auditor — 2층

**기계층 (LLM 없음, 오탐 없음)**:
- 모든 factual Claim에 CITES ≥ 1
- 인용 노드 실존 + 자산 스코프 내
- Claim 텍스트의 수치 ↔ 인용 노드 프로퍼티 수치 대조 (정규식 추출 비교)
- as_of ↔ 인용 노드 published_at 대조
- confidence < 0.5 단독 인용 factual 차단 (§9.3)
- fail → 해당 Writer 즉시 재작성 (위반 목록 첨부)

**LLM층 (Sonnet)**:
- 축간 모순 (tenant "수요 견조" vs commercial "공실 급증")
- 낮은 confidence 증거의 과대 해석, 누락된 반대 증거
- fail → 해당 위치 GAP 마킹 → 미니 EXPAND → Phase 3 재진입 (≤2회)

### 10.3 보고서 구조화 — 주소 가능한 JSON (D12)

보고서의 1급 표현은 markdown이 아니라 **구조화 JSON**이다. markdown은 렌더링
산출물일 뿐이다. 모든 단위에 안정적 ID를 부여해 "보고서의 특정 부분"을
가리킬 수 있게 한다:

```json
{"report_id": "rpt_...", "version_id": "v3",
 "sections": [
   {"section_id": "sec_commercial", "axis": "commercial", "title": "상권 가치 분석",
    "blocks": [
      {"block_id": "blk_c2",                   // 문단 단위
       "markdown": "...",
       "claim_ids": ["clm_17", "clm_18"]}      // 이 문단이 담는 주장
    ]}],
 "claims": {
   "clm_17": {"text": "...", "kind": "factual",
              "cites": ["fact:abc"], "as_of": "2026-04"}},
 "evidence": {                                  // ← 인용된 근거의 전체 해석 정보 (사람 검증용)
   "fact:abc": {
     "text": "강남대로 상권 1분기 공실률 1.2%p 상승",   // 근거 원문 (Fact 문장 / 통계 요약)
     "source_kind": "news",                     // news | public_api | sqlite_stat | user_doc
     "origin": {"url": "https://...", "title": "...", "publisher": "한국경제"},
                                                // sqlite_stat이면 {"table": "...", "row_pk": ...}
     "published_at": "2026-04-02",
     "confidence": 0.8,                         // §9.3
     "validity": {"status": "VALID", "valid_until": null},   // §12, 렌더 시점 평가
     "path": "Asset →[ANCHORS]→ 상권 →[MENTIONS by]→ Fact"   // 어떤 경로로 발견됐나
   }}}
```

**evidence 맵이 핵심이다 (D12 확장).** claims의 `cites`는 ID 참조일 뿐이고,
evidence 맵이 그 ID를 사람이 검증 가능한 수준으로 해석해 둔다 — 근거 원문,
출처(URL/테이블·행), 날짜, confidence, 유효성 상태, 발견 경로까지. 보고서
JSON 하나만 있으면 **문장 단위로 "이 주장은 어디서 왔고 지금도 유효한가"를
사람이 직접 확인**할 수 있다. UI에서 주장 클릭 → 근거 카드가 뜨는 구조의
데이터 기반이며, 수정(§10.4) 시에도 Reviser가 이 맵으로 대상 근거를 즉시
특정한다.

기존 SQLite `report_version.body_json`이 이 구조를 담도록 형식을 확장하고,
그래프에는 Report→Section→Claim이 이미 노드이므로(§4.3) Claim 노드에
block_id 프로퍼티만 추가하면 **JSON ↔ 그래프가 1:1로 맞물린다.** evidence
맵은 그래프에서 렌더 시점에 생성되는 파생물 — 진실은 그래프, JSON은 사람
검증용 스냅샷.

### 10.4 발행자 피드백 루프 — REVISE (D13)

보고서는 생성으로 끝나지 않는다. 발행 담당자가 에이전트와 상호작용하며
보고서 자체를 수정하는 것까지가 목표다. 피드백은 **본문 텍스트 붙여넣기가
아니라 대상 ID + 코멘트**로 들어온다 — UI에서 블록/주장을 클릭하면 ID가
잡히는 구조:

```json
{"target": "clm_17",            // section / block / claim 어느 수준이든
 "kind": "challenge",           // edit | challenge | enrich
 "comment": "공실률 2분기 연속 상승 근거가 약함. 최신 분기 확인 필요"}
```

피드백은 `(:Feedback)-[:TARGETS]->(Claim|Block|Section)` 노드로 그래프에
적재된다 — **사람의 판단도 그래프의 1급 시민.** Reviser가 kind별 처리:

| kind | 의미 | 처리 | 사용 도구 |
|---|---|---|---|
| **edit** | 표현/구성 수정 | 해당 block만 재작성, 인용 유지 | Writer 재호출 (타깃 한정) |
| **challenge** | 사실 이의 | 해당 Claim의 CITES 증거 유효성 재판정(§12) → 미달이면 미니 EXPAND로 최신 증거 수집 후 재작성 또는 철회 | ValidityCheck + 미니 EXPAND |
| **enrich** | 보강 요청 | 대상 주변 walk + 미니 EXPAND → 해당 섹션 증보 | 미니 EXPAND + Writer |

### 10.5 합의 프로토콜 — 토의 후 반영 (D14)

에이전트는 발행자의 피드백·첨부 자료를 **전적으로 신뢰하지 않는다.** 피드백이
도착해도 즉시 보고서를 고치지 않고, 먼저 그래프 증거와 대조한 평가를 회신한다:

```
발행자: Feedback(target, kind, comment) [+ 첨부 자료]   status=open
   ↓
Reviser 평가:
  ① 첨부가 있으면 UserDoc 인입 → 일반 증거와 동일 파이프라인
     (Fact 추출 → 연결(D9) → confidence → validity(D10)) 통과
  ② 피드백 주장 vs 기존 그래프 증거 + 신규 추출 Fact 대조
   ↓
Assessment 회신 (그래프에 적재):
  {stance: agree | disagree | partial | inconclusive,
   rationale: "...",
   cites: [지지/모순 증거 노드 ID],      ← 에이전트도 근거 인용 의무
   proposal: "반영 시 이렇게 수정하겠다" (있다면)}
   ↓
발행자: 수용 / 재반박(코멘트) / 추가 자료 첨부 / 철회
   ↓
(여러 라운드 반복 — 전 과정이 Feedback 스레드로 그래프에 남음)
   ↓
status = agreed     → 그때서야 Reviser가 수정 실행 → Auditor → 새 버전
status = withdrawn  → 반영 없음, 스레드 보존
status = disputed   → 미합의 보류 — 보고서 불변, 쟁점 기록 잔존
```

그래프 모델: `(:Assessment)-[:REPLIES_TO]->(:Feedback)`, 라운드마다 노드가
스레드로 쌓인다. **합의(agreed) 전에는 보고서가 절대 변하지 않는다.**

첨부 자료의 신뢰 등급: UserDoc은 `source_tier=provisional`로 시작한다 —
공식 문서(등기, 계약서, 공시)로 확인되거나 기존 증거와 교차 검증되면 상향,
기존 증거와 모순이면 Assessment에서 모순 지점을 인용과 함께 제시한다. 즉
"의뢰자가 준 자료"라는 이유로 자동 신뢰하지도, 자동 배척하지도 않는다.

수정분은 Auditor 기계층을 다시 통과해야 하며, 통과 시 새 report_version
(parent_version_id 체인, 기존 형식)으로 저장된다. **타깃 외 부분은 건드리지
않는다** — 전체 재생성이 아니라 외과적 수정.

Feedback/Assessment 스레드는 영구히 남아 ① 같은 자산의 다음 평가 라운드에
prior signal로 자동 반영되고(frontier 가산), ② 발행자가 반복 지적하는 패턴
(특정 출처 불신 등)이 쌓이면 가중치 표·검증 규칙 튜닝의 입력이 된다.

---

## 11. QA 상세 (D6 + D7 + D11)

```
질문
 → ① 앵커: 스코프 지도 요약 + LLM 힌트 추출 → 벡터 매칭 (§5.6, 자산 스코프 한정 D11)
 → ② walk(anchors, LLMPolicy(질문), max_hops=3)
 → ③ 충분성 판정: 경로 점수 상위 k의 합이 임계 미달 or LLM "부족" 판정
      ├─ 충분 → ④
      └─ 부족 → 미니 EXPAND 자동 발동 (§8.5, UI: "추가 검색 중...")
                → 수집물 적재 → ② 재실행 (1회만)
                ├─ 확보 → ④ (답변에 "신규 수집 근거 — 검증 1회" 라벨)
                └─ 없음 → Question에 GAP_AT + "범위 밖" 정직 응답
 → ④ 답변 생성: EvidencePath 직렬화 → 답변 + 인용(노드 elementId)
      Question/ANSWERED_BY 적재
```

- 보고서 Claim도 그래프에 있으므로 "보고서가 뭐라 했는지"와 "원천 데이터"를
  한 번의 walk로 같이 본다.
- QA 수집물은 그래프에 영구 적재 → 다음 보고서 개정판이 자동 반영. 질문이
  쌓일수록 자산 그래프가 좋아지는 선순환.
- 출처 등급 표시: 보고서 Claim 인용(Auditor 검증 완료) vs 라이브 수집 Fact
  (검증 1회) — 답변에 구분 라벨.

---

## 12. 유효성 관리 — 공용 ValidityCheck (D10)

### 12.1 시간 속성 추출 (Harvester)

Fact 추출 시 텍스트에 **명시된 시간 정보를 속성으로 함께 구조화**한다.
"~까지", "~예정", "한시 적용" 같은 표현이 유효기간의 원천:

```json
{"text": "OO재개발 사업은 2027년 12월 준공 예정이다",
 "published_at": "2026-04-02",       // 보도 시점
 "event_date": null,                  // 사실이 가리키는 과거 시점 (있다면)
 "valid_until": "2027-12-31",        // 명시 유효기간 — 원문에서 추출
 "temporal_kind": "forecast"}         // static | dated | forecast
```

명시 정보가 없으면 valid_until=null로 두고 폴백 규칙(반감기)을 쓴다.

### 12.2 공용 판단 로직

```python
# graphflow/validity.py — Composer·QA·Auditor·Navigator·Reviser가 전부 이 하나를 쓴다
def validity(node, as_of=today) -> Validity:        # {status, score}
    if node.valid_until:                             # ① 명시 유효기간 우선
        return VALID(1.0) if as_of <= node.valid_until else EXPIRED(0.1)
    if node.temporal_kind == "static":               # ② 정적 사실 (인증, 준공연도)
        return VALID(1.0)
    half_life = HALF_LIFE[node.kind]                 # ③ 폴백 — 반감기 감쇠
    return DECAYED(0.5 ** (months_since(node.published_at) / half_life))
    # 뉴스 Fact 6개월 / 분기 통계 12개월
```

### 12.3 사용처 — 보고서·QA·검증·확장이 같은 로직 공유

| 사용처 | 적용 |
|---|---|
| walk 경로 점수 (§5.4) | score × validity.score — **보고서 집필과 QA가 동일하게** |
| Auditor 기계층 (§10.2) | EXPIRED Fact 단독 인용 factual Claim 차단. as_of ↔ 인용 노드 대조 |
| Navigator frontier (§8.1) | EXPIRED/만료 임박 증거만 있는 노드 가산 → **재검색 신호** |
| QA 답변 | 만료 근거 인용 시 "유효기간 경과(~2027-12) — 최신 확인 필요" 라벨 표시 |
| Reviser challenge (§10.4) | 이의 제기된 Claim의 인용 증거 유효성 재판정이 1차 처리 |

오래된/만료 Fact는 삭제하지 않는다 — "작년엔 이랬다"는 이력 가치가 있고,
frontier 가산을 통해 갱신을 유도하는 신호로 쓰인다. 분기 통계 갱신 시 그래프
프로퍼티는 latest로 교체한다 (시계열 원본은 SQLite).

---

## 13. 그래프 정비 — Janitor (D15)

수집이 누적되면 인입 시점 방어(D9)만으로 못 막는 두 오류가 쌓인다:

- **분열 (duplicate)**: 동일 실체가 여러 노드로 — "스타벅스 역삼점" Entity와
  "역삼동 스타벅스" Entity가 따로 존재. 증거가 분산돼 walk가 둘 다 못 줍거나
  반쪽만 줍는다.
- **오병합 (conflation)**: 다른 실체가 한 노드로 — 동명의 다른 건물이 병합돼
  남의 인증·거래가 우리 자산 근거로 인용된다. **오염이 인용을 타고 보고서까지
  번지므로 분열보다 위험.**

인입 시점엔 판단 정보가 부족해 놓치는 케이스가 있으므로, **주기 점검 배치
(Janitor)** 가 필요하다. LangGraph 워크플로우가 아니라 독립 배치 잡이다.

| 단계 | 내용 |
|---|---|
| 분열 탐지 | 같은 라벨 내 card_text 임베딩 유사 + 속성(주소·좌표·코드) 근접 클러스터링 |
| 오병합 탐지 | 한 노드에 상충 속성(좌표 수 km 차, 주소 불일치), 서로 모순되는 Fact의 집중 |
| 자동 처리 | **코드 수준 근거(bd_mgt_sn·사업자번호·trdar_cd 일치)가 있을 때만** 자동 병합/분리. 병합 시 엣지 이관 + `merged_into` 기록(traceability) |
| 리뷰 큐 | 그 외 의심 케이스는 사람 확인 큐로 — Curator 승격 규칙(§9.2)과 동일한 보수 기준 |
| 주기 | EXPAND 누적 라운드 기준 또는 일/주 배치. 보고서 생성 직전 스코프 한정 점검(빠름) + 전역 주기 점검(느림) 2단 |

Janitor 결과(병합/분리 이력)도 그래프에 남아, 같은 실수를 반복하는 추출
패턴이 보이면 Harvester 연결 규칙(D9) 튜닝의 입력이 된다.

---

## 14. 자동화 수집 트랙 (D16 — 설계는 별도 브랜치)

온디맨드 수집(평가 시 EXPAND, QA 라이브 확장)만으로는 못 잡는 것이 있다:
**LLM 지식 한계 너머의 최근 동향.** 예 — 테넌트 가치 평가에서 앵커 테넌트가
스타벅스인데 최근 본사 차원의 악재가 있었다면, LLM도 모르고 자산 발 멀티홉
검색어("OO타워 임대", "강남대로 상권 공실")도 그것을 정조준하지 못한다.

방향: 스케줄 수집(Airflow 등)이 **자산과 무관하게** 최근 동향을 전역 층(D5)에
선적재한다. 공유 노드(Industry, Entity-프랜차이즈, CommercialArea, AdminDong)에
붙은 수집물은 어느 자산이든 해당 엔티티를 N홉 안에서 만나는 순간 자동으로
스코프에 들어온다 — **전역 층 구조가 이미 받침이라 graphflow 쪽 추가 작업이
거의 없다.**

**수집 대상 선정 자체를 그래프가 결정한다**:

| 대상 | 선정 기준 | 예 |
|---|---|---|
| 허브 엔티티 | degree 상위 공유 노드 (여러 자산에 연결) | 주요 프랜차이즈, 핵심 상권, 반복 등장 개발사업 |
| 갱신 필요 노드 | ValidityCheck상 EXPIRED/만료 임박 Fact 보유 (§12 재검색 신호의 배치판) | 완공 예정일 지난 개발사업 |
| 정기 정형 | 분기/월 갱신 공공 데이터 — 기존 빌더에 스케줄만 | REB 임대동향, NTS 사업자통계, MOLIT |
| 거시 (추후) | 금리·정책 — Entity 노드로 | 임대차보호법 개정 |

**통합 원칙**: 수집 진입점만 다르고 적재 규칙은 동일하다 — 스케줄 수집물도
Harvester와 같은 추출 → 연결(D9) → confidence(§9.3) → validity(§12) 파이프라인을
통과하고 `collected_by="scheduled:{job_id}"`로 표기된다. 검증 안 된 데이터가
뒷문으로 들어오는 경로는 없다.

미정 (별도 브랜치에서 설계): 주기, 소스별 쿼터, Airflow vs 경량 스케줄러,
수집 예산. 데이터 축 작업이므로 graphflow 브랜치와 분리한다.

---

## 15. 공유 vs 신규 vs 삭제

| 구분 | 대상 |
|---|---|
| **공유 (데이터 축)** | LocationResolver, `real_estate/{asset,database,external,repository}`, `infra/` 전체, 데이터 검증 `scripts/try_*` |
| **신규** | `real_estate/graphflow/`: traversal.py, profiles.py, policies.py, validity.py, seeder.py, navigator.py, harvester.py, curator.py, composer.py, writer.py, synthesizer.py, auditor.py, reviser.py, janitor.py, qa.py, state.py, workflow.py, graph/(driver·schema·cards — 스냅샷 복원+확장), llm.py(복원), tracing(복원) + `scripts/try_graphflow.py` |
| **삭제됨 (b7a9e54)** | 구 `real_estate/report/` 전체 + report 의존 스크립트 9개 |

복원 출처: `yachom/wip/v3-pre-graphflow-snapshot` — graph ETL, llm.py,
embedder/vector_index, scout SQL(Seeder로 흡수), report 저장 스키마.

---

## 16. 구현 단계 (PR 단위)

| PR | 내용 | 검증 게이트 |
|---|---|---|
| **A** | 스키마 확장 + Seeder + 카드/벡터 인덱스 (graph 기반 복원 포함) | `try_graphflow.py --seed-only`: 주소 1건 → 그래프 모양/카드 눈검사 |
| **B** | walk 엔진 + StaticPolicy + 프로파일 + 앵커 벡터 매칭 | golden 질문 10개 → 경로 품질 눈검사. 같은 입력 2회 → 동일 출력 확인 |
| **C** | EXPAND 루프 (Navigator/Harvester/Curator + LLMPolicy + novelty) | 검색어 샘플 30개 리뷰(경로 고유명사 포함률), 오연결 0건 확인 |
| **D** | Composer/Writer/Synthesizer/Auditor + Report 서브그래프 + 저장 | Auditor 기계층 위반 0으로 수렴하는지, 보고서 1건 전체 생산 |
| **E** | QA + 미니 EXPAND + GAP 피드백 (+ UI는 후속) | golden 질문 20개: 인용 정확도 + 라이브 확장 발동률/지연 측정 |
| **F** | 보고서 구조화 JSON(evidence 맵 포함) 렌더 + Feedback/Reviser 합의 루프(§10.5) | 피드백 3종 시나리오 각 1건 + **타깃 외 블록 불변** + 미합의 시 보고서 불변 확인. 첨부는 텍스트/URL 먼저, PDF 파서는 후반 |
| **G** | Janitor (그래프 정비, §13) — 탐지 + 리뷰 큐 우선, 자동 병합 최소 | 의도적 중복/오병합 주입 → 탐지율 확인. 자동 병합은 코드 근거 케이스만 |

자동화 수집(D16, §14)은 이 브랜치 범위 밖 — 별도 브랜치 트랙으로 설계/구현.

---

## 17. 리스크 / 미해결

- **Neo4j 운영 전제**: graphflow에선 필수 컴포넌트. 로컬 dev용 docker-compose
  구성 필요 (PR-A에서).
- **비용**: Harvester의 Fact 추출이 검색 결과 수에 비례. Haiku 추출 + Sonnet
  스팟 검증 2단으로 낮출 수 있는지 PR-C에서 측정.
- **QA 라이브 확장 지연**: 검색+스크랩+추출 10초~1분. UI 상태 표시로 1차 대응,
  체감 개선(스트리밍/부분 응답)은 후속.
- **상가 직접 데이터 빈약**: 본 건물 직접 증거가 적을수록 멀티 홉 확장이 유일한
  차별화 — Navigator 검색어 품질이 보고서 품질의 상한. PR-C 리뷰 게이트가 핵심.
- **LLMPolicy 비결정성**: QA 응답이 재실행 시 달라질 수 있음 — 판정 이유 trace로
  보완하되, 문제 되면 plan 기반 StaticPolicy 폴백(§5.7) 검토.
- **미정**: Streamlit UI 재구축 범위, 웹 일반 검색 클라이언트, GraphRAG
  community 요약 도입 여부 (추상 질문 보강용 — Claim 앵커로 1차 대응 후 판단).
