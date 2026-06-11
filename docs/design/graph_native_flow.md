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
| D10 | 신선도 | 삭제 대신 **반감기 점수 감쇠** + Writer 날짜 명시 의무. 오래된 Fact는 재검색 신호로 기능 |

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

### 원칙 5 — 오래된 증거는 삭제하지 않고 감쇠시킨다 (D10)

Fact/WebDoc은 반감기 기반 점수 감쇠를 받는다. 오래된 Fact는 답을 오염시키는 게
아니라 walk에서 후순위로 밀리고, Navigator의 frontier 점수에서 "갱신 필요"
신호로 기능한다 (§12).

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
| `Fact` | fact_id | Harvester | **원자적 사실 1문장** (D3). text, confidence(§9), published_at, collected_by |
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
            × 신선도(끝 노드, §12 반감기 decay)
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
                  보고서 저장 (SQLite + md sidecar, 기존 형식)
                  + Report 서브그래프 적재 (HAS_SECTION/HAS_CLAIM/CITES)

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
 "paragraphs": ["...markdown..."],
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

---

## 11. QA 상세 (D6 + D7)

```
질문
 → ① 앵커: 스코프 지도 요약 + LLM 힌트 추출 → 벡터 매칭 (§5.6)
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

## 12. 신선도 관리 (D10)

- walk 점수에 감쇠 항: `decay = 0.5 ^ (경과개월 / 반감기)`
  - 뉴스 Fact: 반감기 6개월 / 분기 통계: 12개월 / 정적 사실(인증, 준공): 무감쇠
- 자산 B가 자산 A 때의 1년 전 Fact를 만나면: walk에서 후순위 + Navigator
  frontier 점수에서 "오래된 증거만 있는 노드" 가산 → **재검색 신호**로 기능.
- Writer는 인용 시 as_of 명시 의무, Auditor 기계층이 대조.
- 분기 통계 갱신 시 그래프 프로퍼티는 latest로 교체 (시계열 원본은 SQLite).

---

## 13. 공유 vs 신규 vs 삭제

| 구분 | 대상 |
|---|---|
| **공유 (데이터 축)** | LocationResolver, `real_estate/{asset,database,external,repository}`, `infra/` 전체, 데이터 검증 `scripts/try_*` |
| **신규** | `real_estate/graphflow/`: traversal.py, profiles.py, policies.py, seeder.py, navigator.py, harvester.py, curator.py, composer.py, writer.py, synthesizer.py, auditor.py, qa.py, state.py, workflow.py, graph/(driver·schema·cards — 스냅샷 복원+확장), llm.py(복원), tracing(복원) + `scripts/try_graphflow.py` |
| **삭제됨 (b7a9e54)** | 구 `real_estate/report/` 전체 + report 의존 스크립트 9개 |

복원 출처: `yachom/wip/v3-pre-graphflow-snapshot` — graph ETL, llm.py,
embedder/vector_index, scout SQL(Seeder로 흡수), report 저장 스키마.

---

## 14. 구현 단계 (PR 단위)

| PR | 내용 | 검증 게이트 |
|---|---|---|
| **A** | 스키마 확장 + Seeder + 카드/벡터 인덱스 (graph 기반 복원 포함) | `try_graphflow.py --seed-only`: 주소 1건 → 그래프 모양/카드 눈검사 |
| **B** | walk 엔진 + StaticPolicy + 프로파일 + 앵커 벡터 매칭 | golden 질문 10개 → 경로 품질 눈검사. 같은 입력 2회 → 동일 출력 확인 |
| **C** | EXPAND 루프 (Navigator/Harvester/Curator + LLMPolicy + novelty) | 검색어 샘플 30개 리뷰(경로 고유명사 포함률), 오연결 0건 확인 |
| **D** | Composer/Writer/Synthesizer/Auditor + Report 서브그래프 + 저장 | Auditor 기계층 위반 0으로 수렴하는지, 보고서 1건 전체 생산 |
| **E** | QA + 미니 EXPAND + GAP 피드백 (+ UI는 후속) | golden 질문 20개: 인용 정확도 + 라이브 확장 발동률/지연 측정 |

---

## 15. 리스크 / 미해결

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
