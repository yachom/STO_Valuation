# 그래프 네이티브 에이전트 흐름 설계 (graphflow)

> 브랜치: `yachom/feature/graph-native-flow`
> 상태: 설계 초안 (2026-06-11)
> 전제: 기존 에이전트(planner/analyst/critic/… v1~v3)는 **무시**하고, 데이터 레이어만 공유해서
> 그래프 DB 중심의 새 에이전트 흐름을 설계한다.

---

## 1. 목표

**입력**: 주소 1줄.
**출력**: STO 기초자산(상가 부동산) 감정평가 보고서 + 동일 그래프 기반 QA.

두 가지 핵심 요구사항:

1. **QA = 보고서 생성과 동일한 과정.**
   확장된 데이터를 그래프 DB(Neo4j)에 저장하고, 보고서 생성도 그래프의 노드/엣지를
   타고, QA도 노드/엣지를 타고 진행한다. 둘이 같은 순회(traversal) 엔진을 쓰면
   보고서가 답할 수 있는 것은 QA도 답할 수 있고, QA가 못 답하는 것은 그래프의
   공백(gap)으로 명시적으로 드러난다.

2. **멀티 홉 그래프 기반 데이터 확장.**
   네이버 검색 등 외부 검색 도구의 검색어를 LLM이 허공에서 짓는 게 아니라,
   그래프에서 자산 노드로부터 노드 간 관계(엣지)를 따라가며 "중요 속성·중요 연결
   엔티티"를 발견하고, 그 **경로(path) 자체를 검색어의 재료**로 쓴다.
   검색 결과는 다시 그래프에 노드/엣지로 적재되어 다음 홉의 출발점이 된다.

---

## 2. 현재 코드 요약 (입력 → 흐름 → 데이터 → 산출물)

새 설계가 무엇을 공유하고 무엇을 버리는지 기준선.

### 2.1 입력 처리 (공유 ✅)

```
주소 문자열
  → LocationResolver (real_estate/location.py:154) : 카카오 주소검색 + 좌표→행정구역, Juso API
  → LocationInfo : 좌표, sigungu, hcode/bcode, bd_mgt_sn(건물관리번호 25자리), bun/ji
  → AssetContext (tool/retriever/sqlite_retriever.py:32) : asset_id, 좌표, bd_mgt_sn, sigungu_cd
```

### 2.2 데이터 레이어 (공유 ✅)

- **SQLite 진실 저장소** `real_estate/database/DB/real_estate.db` — 25+ 테이블:
  MOLIT 실거래(아파트 매매/전월세, 상가 매매), BEEC/G-SEED 인증, 서울 상권
  (유동/거주인구·추정매출, 행정동 폴백), REB 임대동향, NTS 사업자통계, KB 시세,
  정비사업, 지하철/버스.
- **외부 클라이언트** — `infra/external/` (naver_search, naver_news_scraper,
  public_data_portal, seoul_open_data), `real_estate/external/molit_rtms`.
- **Neo4j ETL 기초** — `real_estate/report/graph/{schema,ingest,cards,driver}.py`:
  노드 라벨/엣지 타입/UNIQUE constraint 정의, SQLite→Neo4j UNWIND+MERGE 적재,
  노드별 한국어 card_text 템플릿. **Report/ReportSection/Claim + HAS_SECTION/
  HAS_CLAIM/CITES 라벨이 이미 예약**되어 있음 (schema.py:33-54).

### 2.3 기존 에이전트 흐름 (무시 ❌ — 참고용)

- v2: planner → 3축 analyst(3-step scout) 병렬 → synthesizer → risk_critic
  →(fail) reviser 루프 → final_editor → validator.
- v3: planner_v3(Co-STORM mind_map + prefetch 11종) → 동일 analyst → critic
  →(fail) reflection → replan 루프.
- QA: 보고서에 박힌 evidence_snippets만 임베딩하는 ReportScopedRetriever + 단발 LLM.
  → **보고서 생성 경로와 QA 경로가 완전히 다름.** mind_map은 state 안 임시 dict로,
  보고서가 끝나면 버려짐. 이 비대칭이 이번 설계가 없애려는 것.

### 2.4 산출물 (형식 유지 ✅)

SQLite `report`/`report_version` + `reports/{report_id}/{version_id}.md` sidecar
+ `trace.jsonl`. 새 흐름도 같은 저장 형식을 쓰되, **그래프에도 Report 서브그래프를
적재**해 보고서 자체가 그래프의 1급 시민이 된다.

---

## 3. 핵심 설계 원칙

### 원칙 1 — 그래프 = 단일 작업 메모리 (single working memory)

v3의 mind_map(임시 dict), evidence_pool(임시 dict), prefetched_facts(임시 dict)가
하던 일을 전부 Neo4j가 한다. 에이전트의 모든 산출물(수집한 사실, 추출한 엔티티,
생성한 주장)은 노드/엣지로 적재되고, 모든 입력은 그래프 순회로 얻는다.
재실행/리플랜/QA가 전부 같은 그래프를 보므로 상태 동기화 문제가 사라진다.

### 원칙 2 — 단일 순회 프리미티브 (GraphWalk)

확장(어디를 더 팔까), 집필(이 섹션의 근거는 뭔가), QA(이 질문의 근거는 뭔가)는
모두 같은 질문이다: **"앵커 노드에서 출발해 의미 있는 경로를 따라 모은 증거는
무엇인가."** 이를 하나의 엔진으로 구현한다:

```python
# graphflow/traversal.py (신규)
def walk(
    anchors: list[NodeRef],        # 출발 노드 (자산 노드 or 질문 매칭 노드)
    *,
    max_hops: int = 3,
    edge_weights: dict[str, float],  # 엣지 타입별 가중치 (축/질문별 프로파일)
    k: int = 12,
) -> list[EvidencePath]

@dataclass
class EvidencePath:
    nodes: list[NodeCard]      # card_text 체인 (cards.py 재사용)
    edges: list[EdgeRef]       # 엣지 타입 + elementId
    score: float               # 경로 점수 (가중치 × 신선도 × 신뢰도)
    text: str                  # LLM 컨텍스트용 직렬화: "A —[LOCATED_IN]→ B —…"
```

- **Navigator**(확장)는 walk 결과에서 "증거가 빈약한 경로 끝(frontier)"을 고른다.
- **Composer**(집필)는 walk 결과를 섹션 증거로 직렬화한다.
- **QA**는 질문→앵커 매칭 후 같은 walk를 돈다.

→ 요구사항 1이 구조적으로 충족된다: 보고서와 QA가 같은 엔진·같은 그래프를 쓴다.

### 원칙 3 — 경로가 검색어를 만든다 (path-grounded query expansion)

검색어는 항상 `(자산 → … → 타깃 노드)` 경로에서 유도된다. 예:

```
(Building "OO타워")-[:LOCATED_IN]->(AdminDong "역삼1동")
                                    <-[:SERVES_DISTRICT]-(CommercialArea "강남대로 상권")
  → "강남대로 상권 공실률 2026", "역삼동 상가 임대 시세"

(CommercialArea "강남대로 상권")-[:TOP_INDUSTRY]->(Industry "커피전문점")   # 2-hop
  → "강남대로 커피 프랜차이즈 출점", "역삼동 카페 폐업"

(Building)-[:CERTIFIED_AS]->(Certification "BEEC 1+")                     # 1-hop
  → "건물 에너지효율 1등급 임대료 프리미엄"
```

홉이 깊어질수록 검색어가 자산의 구체 맥락(상권 이름, 주력 업종, 인증 등급,
정비사업명)을 품게 된다 — LLM 단독 브레인스토밍으로는 못 만드는 검색어.
모든 검색은 `(:Query)` 노드로 기록되어 **어떤 경로가 이 검색을 낳았는지**
provenance가 남는다 (중복 검색 방지 + 확장 이력 감사).

### 원칙 4 — 모든 주장은 그래프에 닿아야 한다 (claim grounding)

보고서의 문장 단위 주장(Claim)은 노드로 적재되고 `CITES` 엣지로 증거 노드에
연결된다. Auditor는 "CITES 없는 Claim"을 기계적으로 잡아낸다(LLM 불필요).
QA 답변도 같은 방식으로 인용한다.

---

## 4. 그래프 스키마 확장

기존 `schema.py`의 라벨/엣지에 추가한다 (기존 것은 그대로 재사용):

### 4.1 신규 노드 라벨

| 라벨 | 키 | 출처 | 설명 |
|---|---|---|---|
| `Asset` | asset_id (`re:{bd_mgt_sn}`) | Seeder | 평가 대상 자산. 모든 walk의 기본 앵커 |
| `WebDoc` | url_hash | Harvester | 네이버 뉴스/블로그/웹 문서. title, url, published_at, source |
| `Fact` | fact_id | Harvester | WebDoc에서 LLM이 추출한 원자적 사실 1문장. text, confidence |
| `Entity` | entity_id (정규화명) | Harvester/Curator | 추출된 엔티티 (프랜차이즈, 개발사업, 정책 등). 기존 라벨에 매칭 실패한 것만 |
| `Industry` | industry_cd | Seeder(SQLite) | 상권 업종 (commercial_estimated_sales의 svc_induty_cd) |
| `Query` | query_hash | Navigator | 실행한 검색어. text, executed_at, hop_path, n_results |
| `Question` | question_id | QA | 사용자 질문. answered 여부, gap 여부 |

### 4.2 신규 엣지 타입

| 엣지 | 방향 | 설명 |
|---|---|---|
| `ANCHORS` | Asset → Building/AdminDong/CommercialArea/LegalDong | 자산의 구조적 위치 |
| `TOP_INDUSTRY` | CommercialArea → Industry | 상권 주력 업종 (매출 상위 N) |
| `SPAWNED_QUERY` | 노드 일반 → Query | 이 노드(경로 끝)가 낳은 검색 |
| `RETRIEVED` | Query → WebDoc | 검색 결과 |
| `EXTRACTED_FROM` | Fact → WebDoc | 추출 provenance |
| `MENTIONS` | Fact → 노드 일반 | 사실이 언급하는 기존 노드 (그래프 연결의 핵심) |
| `ABOUT` | WebDoc → Entity/기존 노드 | 문서 주제 |
| `ASKED` | Question → Report | QA 이력 |
| `ANSWERED_BY` | Question → Claim/Fact | 답변 근거 |
| `GAP_AT` | Question → 노드 일반 | 못 답한 질문이 가리키는 그래프 공백 위치 |

### 4.3 기존 라벨 재사용

`Report`, `ReportSection`, `Claim` + `HAS_SECTION`, `HAS_CLAIM`, `CITES`는
schema.py에 이미 예약돼 있음 — 이번에 실제로 채운다. `card_text`(cards.py)는
신규 라벨에도 빌더를 추가해 전 노드가 의미 검색 가능하게 유지.

### 4.4 자산 스코프 (중요)

전체 SQLite를 통째로 적재(현 ingest_all)하지 않고, **자산 1건 기준 스코프 적재**를
기본으로 한다: `Asset` 앵커에서 닿는 것만 — 본 건물(bd_mgt_sn), 소속 법정동/행정동,
point-in-polygon 상권 + 인접 상권, 반경 800m 단지/지하철/버스, 자치구 정비사업,
시군구 단위 시장 통계 요약. 시계열 raw는 기존 원칙대로 SQLite에 두고, 그래프엔
최신 분기 요약을 프로퍼티로 박는다.

---

## 5. 새 에이전트 구성 (roster)

LLM을 쓰는 곳과 안 쓰는 곳을 명확히 분리한다.

| # | 에이전트 | LLM | 역할 |
|---|---|---|---|
| 1 | **Seeder** | ❌ | 주소 → LocationResolver → Asset 노드 + 자산 스코프 구조 적재(§4.4) + card_text 생성. 결정적, 재실행 안전 |
| 2 | **Navigator** | ✅ Sonnet | walk로 frontier 후보(증거 빈약 경로 끝) 수집 → 확장할 타깃 M개 선택 + 경로 기반 검색어 생성(§3 원칙 3). Query 노드 적재 |
| 3 | **Harvester** | ✅ Sonnet | Query 실행(naver_search/news_scraper) → WebDoc 적재 → LLM이 Fact/Entity 추출 → MENTIONS 엣지로 기존 노드에 연결 |
| 4 | **Curator** | ✅ Haiku | 엔티티 dedup/병합(임베딩 유사도 + LLM 판정), 신규 노드 card_text, 노드 importance 점수 갱신 |
| 5 | **Composer** | ❌ | 섹션별 edge_weights 프로파일로 walk → EvidencePath 묶음 직렬화. Cypher + 점수 계산만 |
| 6 | **Writer** | ✅ Sonnet | 섹션 본문 작성. 문장 주장마다 Claim 노드 + CITES 엣지 적재 (structured output으로 claim↔evidence 매핑 강제) |
| 7 | **Synthesizer** | ✅ Opus | 섹션 통합 Overview + 감정평가 결론(가치 논거). Claim grounding 동일 적용 |
| 8 | **Auditor** | 반반 | (기계) CITES 없는 Claim, 끊긴 경로 탐지 → (LLM) 모순/과추정 검토. fail이면 해당 위치를 GAP으로 마킹 → Navigator 재호출 |
| 9 | **QA Responder** | ✅ Sonnet | 질문 → card_text 벡터 매칭으로 앵커 결정 → **Composer와 동일한 walk** → 답변 + Claim/Fact 인용. 근거 없으면 Question에 GAP_AT 마킹 |

기존 대비: planner가 없다. **outline을 미리 짜지 않고, 그래프가 커진 뒤 그래프의
모양(어느 축에 증거가 모였나)이 섹션 구성을 결정**한다. 섹션 골격은 감정평가
보고서 표준 목차(자산 개요 / 입지·상권 / 수익성·임대시장 / ESG·물리적 상태 /
리스크 / 가치 결론)를 고정 틀로 두되, 각 섹션의 세부 논점은 walk 결과에서 나온다.

---

## 6. 워크플로우 (LangGraph)

```
                ┌──────────────────────────────────────────┐
                │ Phase 1: SEED                            │
 주소 ─────────▶│ Seeder: Asset 앵커 + 구조 서브그래프 적재 │
                └──────────────┬───────────────────────────┘
                               ▼
                ┌──────────────────────────────────────────┐
                │ Phase 2: EXPAND (멀티 홉 루프, R라운드)    │
                │  ┌→ Navigator: frontier 선택+검색어 생성  │
                │  │   Harvester: 검색→WebDoc→Fact/Entity  │
                │  │   Curator:   dedup+card+점수 갱신      │
                │  └─ 신규성(novelty) ≥ θ & round < R ──┐   │
                └──────────────┬────────────────────────┘   │
                               ▼ (novelty < θ or budget 소진)
                ┌──────────────────────────────────────────┐
                │ Phase 3: COMPOSE & WRITE (섹션 병렬)      │
                │  Composer(walk) → Writer(Claim+CITES)    │
                │  → Synthesizer(Overview+가치결론)         │
                └──────────────┬───────────────────────────┘
                               ▼
                ┌──────────────────────────────────────────┐
                │ Phase 4: AUDIT                           │
                │  기계 검증(CITES) + LLM 검토              │
                │  fail → GAP 마킹 → Phase 2 재진입(최대 2회)│
                └──────────────┬───────────────────────────┘
                               ▼
                  보고서 저장 (SQLite + md sidecar)
                  + Report 서브그래프 적재 (HAS_SECTION/HAS_CLAIM/CITES)

  ───────────── QA (별도 entry, 같은 그래프·같은 walk) ─────────────
  질문 → 앵커 매칭 → walk → 답변(+인용) │ 근거 없음 → GAP_AT 마킹
  GAP 누적분은 다음 보고서 라운드의 Phase 2 frontier에 자동 합류
```

### 6.1 EXPAND 루프 제어 (budget)

- 라운드 R ≤ 3, 라운드당 frontier M ≤ 8 노드, 노드당 검색어 ≤ 2, 총 검색 ≤ 40.
- **novelty 측정**: 라운드 종료 시 `신규 Fact 중 기존 Fact와 임베딩 유사도 < 0.85인
  비율`. θ=0.3 미만이면 조기 종료(더 파도 새 정보가 안 나옴).
- frontier 점수 = `경로 가중치 × (1 / (1+기존 증거 수)) × 신선도` — "자산과 가깝고,
  증거가 빈약하고, 시의성 있는" 노드부터 판다.
- 검색어 dedup: Query.query_hash + 임베딩 유사도로 기실행 검색 회피.

### 6.2 state (LangGraph) 최소화

그래프가 작업 메모리이므로 state는 포인터만 남긴다:

```python
class GraphFlowState(TypedDict):
    asset_id: str            # 그래프 앵커
    report_id: str
    round: int               # expand 라운드 카운터
    novelty: float           # 직전 라운드 신규성
    audit_iter: int          # audit 재진입 카운터
    sections_done: list[str] # 병렬 머지용
```

mind_map/evidence_pool/prefetched_facts 같은 대형 dict가 state에서 사라진다 —
전부 그래프 조회로 대체.

---

## 7. QA 상세 — 보고서와 동일 과정의 의미

1. 질문 임베딩 → 전 노드 card_text 벡터 인덱스에서 앵커 top-3 매칭
   (기존 vector_index.py 재사용).
2. 앵커에서 `walk(anchors, edge_weights=qa_profile)` — Composer가 섹션 쓸 때와
   같은 함수, 같은 그래프. 보고서가 인용한 Claim도 그래프에 있으므로
   "보고서가 뭐라고 했는지"와 "원천 데이터가 뭐라고 하는지"를 한 번에 순회.
3. 답변 생성: EvidencePath 직렬화 → Sonnet → 답변 + 인용(node elementId).
4. 근거 경로가 없으면: `(:Question)-[:GAP_AT]->(가장 가까웠던 노드)` 적재 후
   "범위 밖" 응답. **이 GAP 노드들이 다음 expand 라운드의 frontier에 합류** —
   v3의 prior_qa_gaps 개념이 그래프 안으로 들어온 것.

---

## 8. 공유 vs 신규 vs 삭제

> 2026-06-11 결정: 이 브랜치에서 `real_estate/report/` 전체와 report 의존
> 스크립트를 **삭제**했다 (커밋 b7a9e54). 데이터 축만 기존과 동일하게 유지.
> 삭제 직전 워킹트리의 v2/v3 작업분은 `yachom/wip/v3-pre-graphflow-snapshot`
> 브랜치에 보존 — 필요한 코드는 거기(또는 main)에서 선별 복원한다.

| 구분 | 대상 |
|---|---|
| **공유 (데이터 축, 그대로)** | LocationResolver(`real_estate/location.py`), `real_estate/{asset,database,external,repository}`, `infra/` 전체(env/gateway/external/asset/repository), 데이터 검증용 `scripts/try_*` |
| **신규** | `real_estate/graphflow/` 패키지: traversal.py, seeder.py, navigator.py, harvester.py, curator.py, composer.py, writer.py, synthesizer.py, auditor.py, qa.py, state.py, workflow.py + `scripts/try_graphflow.py`. 그래프 인프라(driver/schema/cards)·임베딩·llm 매니저·트레이싱·report 저장은 스냅샷/main에서 선별 복원해 graphflow 밑에 재배치 |
| **삭제 (이 브랜치)** | `real_estate/report/` 전체 — agents, workflow(graph_v1~v3), tool(retriever/scout/mindmap), graph(Neo4j ETL), storage, streamlit_app, llm.py, tracing, domain + report 의존 스크립트 9개 |

> scout 12종의 SQL 로직은 개념적으로 버리지 않는다 — Seeder의 스코프 적재(§4.4)가
> 같은 쿼리를 재사용해 그래프 프로퍼티/노드로 변환한다. "에이전트가 매번 SQL
> 도구를 부르는" 패턴이 "그래프에 미리 구워두고 순회하는" 패턴으로 바뀌는 것.
> 복원 시 출처: 스냅샷 브랜치의 `tool/scout/data_scout.py`, `graph/*`, `llm.py`.

---

## 9. 구현 단계 (PR 단위 제안)

1. **PR-A: 스키마 확장 + Seeder** — 신규 라벨/엣지/constraint, 자산 스코프 적재,
   신규 card 빌더. `try_graphflow.py --seed-only`로 그래프 모양 검증.
2. **PR-B: GraphWalk 엔진** — traversal.py + edge_weights 프로파일 + 경로 직렬화.
   벡터 앵커 매칭 포함. 단독 try 스크립트로 경로 품질 눈검사.
3. **PR-C: EXPAND 루프** — Navigator/Harvester/Curator + budget/novelty 제어.
   여기서 네이버 검색 확장 품질이 판가름 — 검색어 샘플 리뷰 게이트.
4. **PR-D: COMPOSE/WRITE/AUDIT** — Composer/Writer/Synthesizer/Auditor +
   Report 서브그래프 적재 + 기존 저장 형식 연결.
5. **PR-E: QA + GAP 피드백** — QA Responder, GAP_AT, Streamlit QA 탭 교체.

---

## 10. 리스크 / 미해결 질문

- **엔티티 해소(entity resolution)**: Harvester가 추출한 "강남 OO타워"를 기존
  Building 노드에 잘못 연결하면 오염이 경로를 타고 번진다. Curator의 병합 판정에
  보수적 기준(주소/코드 일치 우선, 이름 유사도 단독으론 별도 Entity 유지) 필요.
- **WebDoc 신뢰도**: 블로그/뉴스의 사실성 등급(source tier)을 Fact.confidence에
  반영하고, Writer가 낮은 confidence 증거만으로 Claim을 만들지 못하게 제한.
- **Neo4j 운영 전제**: 지금은 선택적 컴포넌트였으나 graphflow에선 필수가 된다.
  로컬 dev용 docker-compose 또는 embedded 대안 결정 필요.
- **비용**: expand 루프의 LLM 추출 호출이 검색 결과 수에 비례. Haiku 추출 +
  Sonnet 판정의 2단 구성으로 낮출 수 있는지 PR-C에서 측정.
- **상가 직접 데이터 빈약**: 본 건물의 직접 증거(상가 매매 1~2건, 인증 유무)가
  적을 때 멀티 홉 확장이 사실상 유일한 차별화 데이터 — Navigator 검색어 품질이
  보고서 품질의 상한을 결정한다.
