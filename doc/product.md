# 01. 프로젝트 본질 — 무엇을, 왜, 누가, 어떻게

## 1. 한 줄 요약

**STO (Security Token Offering, 토큰증권) 기초자산의 감정평가(Sentiment Valuation)를 LLM 온톨로지로 정형화하는 파이프라인.**

## 2. 문제 정의

### 2.1 시장 관찰

기존 조각투자/토큰증권 시장에서 **발행가와 시장가의 괴리**가 크다.

### 2.2 가설

이 괴리는 **사회적·비정형·맥락적 가치**가 평가 과정에서 누락되기 때문이다. 기존 평가는 정량(수익률·매출)과 구조(법적 안정성)에만 집중하며, **감정(시장 심리, 정책 변화, 임차인 업황, 지역 개발 호재·악재)**을 시스템적으로 반영하지 못한다.

### 2.3 본 프로젝트의 처방

**감정평가축을 LLM 온톨로지로 정형화**한다. 즉:
- 뉴스·이벤트·정책 등 비정형 텍스트를 → **온톨로지** 로 태깅. 단, 현재는 어떤 기준과 어떤 축으로 온톨로지를 구축할 지 정하지 않음. 
- 태깅 결과를 **지식 그래프** 에 적재
- 자산 평가 시 그래프를 traversal 하여 **evidence 기반 평가 리포트** 생성

이로써 "감정"이라는 모호한 차원이 **추적 가능하고 비교 가능한 지표**가 된다.

## 3. 평가 3축 (Evaluation Dimensions)

모든 STO 기초자산은 다음 3축으로 평가된다:

| 축 | 의미 | POC 상태 | 본 프로젝트 비중 |
|---|---|---|---|
| **정량 (Quantitative)** | 수익률, 매출, 거래량 등 수치 지표 | Stub (score=0.0) | 기존 방식 차용, 현재 프로젝트의 구현 대상 아님 |
| **구조 (Structural)** | 법적 안정성, 권리관계, 토큰 구조 | Stub (score=0.0) | 기존 방식 차용, 현재 프로젝트의 구현 대상 아님 |
| **감정 (Sentiment)** | 사회적·맥락적 신호 (뉴스, 정책, 업황) | **실 구현** | **본 프로젝트 핵심** |

3축 모두 자산별로 평가 요인 (`EvaluationFactor`) 이 다르다. 예: 상가 부동산의 정량평가는 `임대수익률`/`공실률`/`거래량`, 음악 저작권은 `스트리밍 추세`/`정산`.

자산별 평가 요인 사전은 `projects/<자산>/config/ontology_schema.py` 에 정의.

## 4. POC 자산

### 4.1 상가형 부동산 (MVP — 진행 중)

**대상:** 도시 내 상가건물 (오피스 빌딩, 근린생활시설 등)

**평가 단위:**
- `address` — 해당 상가건물의 주소로 식별. 이를 기반으로 여러가지 api 호출해 해당 건물의 메타데이터(pnu 등) 생성 후 분석 
- `BuildingTarget` — PNU(19자리 토지고유번호) 로 식별
- `TenantTarget` — 사업자등록번호 로 식별

**감정평가 입력:**
- 뉴스 (네이버 검색 API) — 시장 동향, 정책, 임차인 업황, 도시계획
- 공적 API (VWorld, MOLIT 실거래가, 건축물대장, 건축hub 등)

**평가 요인 예시 (`COMMERCIAL_EVALUATION_FACTORS`):**
- 임대수익률, 공실률, 거래량, 상권 활성도, 금리 민감도, 임대료 동향, 임차인 안정성 등 여러가지 방면으로 추후 확장 가능, 현재 상세 구현은 구상하지 않음. 다만, 확장 용이한 구조로 설계

### 4.2 음악 저작권 (POC 2 — placeholder)

**대상:** K-pop / 가요 등 저작권 신탁 자산

**평가 단위:**
- `SongTarget` — ISRC (International Standard Recording Code) 로 식별
- `ArtistTarget` — (name, agency) 로 식별

**감정평가 입력:** 미정 (스트리밍 차트, 저작권 분쟁 뉴스 후보)

**평가 요인 예시 (`MUSIC_EVALUATION_FACTORS`):**
- 스트리밍 추세, 저작권 분쟁, 아티스트 활동, 플랫폼 정산 등 추후 확장 가능

### 4.3 자산 추가 원칙

새 자산 추가는 **`core/` 코드 수정 없이** 다음만으로 끝나야 한다:
1. `projects/<새 자산>/` 디렉토리 생성
2. `profile.py` — AssetProfile 인스턴스 정의
3. `targets/` — 평가 단위 클래스 (PNU 같은 unique key 기준)
4. `config/ontology_schema.py` — 자산별 평가 요인 사전
5. (필요 시) `tools/` — 자산 특화 도구
6. `composition.py` 의 PROFILE_REGISTRY 에 등록

상세 절차: [08-poc-scope-and-extension.md](08-poc-scope-and-extension.md).

## 5. 두 실행 흐름 (Two Execution Flows)

### 5.1 배치 흐름 — 자동 수집(Airflow)

#### 5.1.1 뉴스 수집

**목적:** 시장 전반의 감정 신호를 자동으로 누적해 그래프에 적재.

**실행:** 매일 (cron `@daily`). 

**파이프라인:**
```
search_groups.yaml (검색어) 
    → NaverNewsClient.search()    # 외부 API
    → NewsNormalizer.normalize()  # html cleanup, url normalize, hash
    → NewsRepository.save()       # DB 적재 (중복 차단)
    → RuleFilterService           # 키워드 룰 1차 필터
    → EmbeddingService            # 텍스트 임베딩
    → DedupService                # 의미상 중복 제거 (cosine sim ≥ 0.92)
    → OntologyTaggingService      # LLM → OntologyTag (6축 enum)
    → GraphSignalService          # OntologyTag → SignalStore + GraphWriter
```

**결과:** `news_articles`, `ontology_tags`, `market_signals`, `graph_nodes/edges` 테이블에 매일 누적.

#### 5.1.2 기타 자료(차트, 가격 등)

**목적:** 매번 크롤링 하기 비효율적인 데이터 자동 수집 후 db 적재. 추후 해당 db 를 에이전트가 볼 수 있도록 함수화 또는 쿼리화.
* 예 : 1주일 단위로 업데이트 되는 가격 데이터 , 일별 차트 데이터 등

**실행:** 데이터별 상이 

### 5.2 On-demand 흐름 — 주소 분석 (멀티에이전트)

**목적:** 사용자가 특정 자산 (예: "서울 강남구 테헤란로 152" 빌딩) 을 입력하면 내부적 로직 통해 그 자산에 대한 평가 리포트를 즉시 생성.

**실행:** 사용자 요청 시 (CLI `analyze-address`, 또는 API). 해당 요청은 웹 ui 로 만들고, api 백엔드를 연결할 계획

**파이프라인:**
```
주소 입력
    → InitialCollector              # 공적 API (JUSO → VWorld → MOLIT → 건축물대장) 호출해 해당 주소의 메타데이터 수집
    → ExplorationFlow (LangGraph)   # Planner → Retriever → Critic 루프 (max_iter=3)
    │     - Planner: LLM 이 다음 도구 호출 계획 + rationale # 메타데이터 기반 추가 수집해야할 정보 , 수집 계획 수립 
    │     - PolicyGate: max_tool_calls / max_cost / allowed_tools 검사
    │     - Retriever: 도구 실행
    │     - Critic: 결과 평가, 충분하면 stop
    → OntologyMapper                # adapter 직접 매핑 (구조화) 또는 LLM fallback (비정형)
    → GraphStore.upsert()           # scope_id=analysis_<uuid> 로 격리
    → ReportBuilder                 # 그래프 → 리포트 텍스트
    → QAFlow (LangGraph)            # Analyzer → Retriever → Synthesizer → Verifier (max_retries=2)
```

**결과:** `AnalysisResult` 객체 — `claims: list[EvaluationClaim]`, `evidence_trace`, `report_text`.

### 5.3 두 흐름의 연결 지점

**공유 온톨로지 어휘**를 통해 자동 연결.

```
배치 흐름 그래프 노드:     [News] -[GENERATES]-> [Signal] -[AFFECTS]-> [EvaluationFactor: "공실률"]
                                                            -[CONTEXT_OF]-> [AssetClass: "commercial"]
                                                            -[CLASSIFIED_AS]-> [SignalType: "market"]

온디멘드 그래프 노드:     [Address: "..."] -[HAS]-> [Building: PNU] -[LOCATED_IN]-> [Region]
                                                            -[AFFECTED_BY]-> [EvaluationFactor: "공실률"]  ← 공유 노드
```

**같은 `EvaluationFactor` 노드** (예: `"공실률"`) 를 traversal 함으로써 "이 빌딩의 공실률 평가에 영향을 주는 최근 시장 신호" 가 자연스럽게 연결된다. 단, 공실률은 예시로, 배치 흐름에서 가져올 데이터는 해당 주소에 대해 에이전트가 자율 탐색할 때 가져올 수 없는 데이터 (예 : 관악구 봉천동 123 주소를 분석할 때 , 해당 건물이 가진 메타데이터 (예 : 제2종 근린시설) 에 대한 정보를 매번 검색해 가져오는건 비효율적이거나, 가져올 가능성이 낮음) 를 미리 적재해 풍부한 주소를 주기 위함임. 

