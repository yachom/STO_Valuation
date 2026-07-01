# 음악저작권 가치평가 파이프라인 재설계 및 점검

> 작성일: 2026-06-29  
> 대상: `music_copyright/graphflow`  
> 기준 E2E: 우예린 `토끼`, STORM/discourse workflow  
> 목적: 수집부터 보고서와 후속 QA까지 추적 가능한 그래프 기반 평가 체계 확립
>
> **📌 읽는 법(2026-07-01).** **§1~§2(4단계 파이프라인·단계별 계약)와 §5(자율성 경계)는 항구적
> 설계이자 현행**이다. 반면 **§3 문제 표와 §4 `토끼` E2E 수치는 2026-06-29 시점 스냅샷**이다 —
> 여기서 진단된 **P0/P1(질의 템플릿 교체·`source_excerpt` 필수·`EXTRACTED_BY` provenance·work-local
> novelty·report별 namespace·공유 budget)은 이후 반영**됐다(§4 말미가 스스로 밝히듯 계약 추가
> 완료; as-built은 [`entity_resolution_layers.md`](entity_resolution_layers.md)). §4 실행은 *결함
> 발견용 baseline*이지 새 계약의 검증 결과가 아니며, **§6은 그 시점의 재검증 계획**(대부분 수행됨).
> P2 잔여 항목(출처 권위 검증·문서 snippet 한계·reranker의 entailment 겸직 등)은 열린 과제로 남을 수 있다.

## 1. 결론

프로젝트는 다음 네 단계로 이해하는 것이 맞다.

1. 데이터 수집
2. 그래프 적재
3. 그래프 탐색
4. 보고서 생성

다만 실제 제어 흐름은 선형이 아니다. 수집과 적재는 그래프 상태를 갱신하며 반복하고,
보고서 초안에서 증거 공백을 발견하면 제한적으로 다시 수집한다.

권장 경계는 다음과 같다.

```text
식별·정형 시드
  → [ResearchTask 계획 → 채널별 검색 → Fact/Entity 적재 → 성과 피드백] 반복
  → Entity 해소·승격
  → Graph Snapshot/Freeze
  → 읽기 전용 멀티홉 탐색·근거 선별
  → 인용 기반 보고서 생성·기계 QA
  → 제한된 gap 재수집·해당 축 재작성
  → Report/Section/Claim/CITES 그래프 적재
  → 후속 QA
```

핵심은 **그래프 구축 루프와 보고서용 읽기 단계를 분리**하는 것이다. 보고서 작성 중
임의로 그래프가 계속 변하면 같은 보고서 안에서도 축별 snapshot이 달라질 수 있다.

## 2. 단계별 책임과 계약

### 2.1 데이터 수집

입력:

- 평가 대상 `Work`와 performer `Artist`
- 평가 목적과 4개 축
- 현재 그래프 커버리지
- 미해소 Entity와 프론티어
- 이전 질의의 성공·실패 outcome

중간 계약:

- `ResearchTask`: 축, 질문, 필요한 증거, 직접 작품/아티스트 맥락 구분
- `QueryVariant`: 검색어, 채널, 전략, 기대 신호
- `QueryOutcome`: 선택/적재 문서, grounded Fact, 신규 Fact, 폐기 사유, 출처, utility

출력:

- 원문 provenance가 있는 `WebDoc`
- 원자적 `Fact`
- mention에서 발견된 `Entity`
- YouTube 등 공개 정량 `SocialMetric`
- 다음 Planner가 사용할 질의 성과

정책:

- LLM은 조사 질문과 검색 전략을 제안한다.
- 코드는 대상 앵커, 허용 채널, 중복, 예산을 검증한다.
- 잘못된 LLM 질의를 고정 템플릿으로 교체하지 않고 누락 앵커만 보강한다.
- Fact에는 실제 문서에 존재하는 `source_excerpt`가 필수다.

### 2.2 그래프 적재

수집과 중첩되는 이유는 두 가지다.

- 다음 검색 프론티어가 직전 적재 결과에 의존한다.
- 새 Fact가 발견한 Entity와 관계가 다음 조사 질문을 바꾼다.

핵심 provenance:

```text
Work -[:SPAWNED_QUERY {target_ref, task_id}]-> Query
Target -[:SPAWNED_QUERY]-> Query
Query -[:RETRIEVED]-> WebDoc
Fact -[:EXTRACTED_FROM]-> WebDoc
Fact -[:EXTRACTED_BY]-> Query
Fact -[:MENTIONS]-> Work|Artist|TypedNode|Entity
```

`EXTRACTED_BY`가 없으면 같은 문서를 여러 축에서 검색했을 때 Fact가 어느 조사 과제에서
추출됐는지 알 수 없다. 축별 coverage와 후속 QA도 잘못된다.

수집 단계의 `Query`, `Fact`, 미해소 `Entity` ID는 Work ID를 포함한다. 이 노드들을
이름이나 문장만으로 전역 공유하면 동일 아티스트·일반 엔티티를 통해 다른 Work의 수집
corpus가 연결된다. 전역 공유는 해소가 끝난 `Artist`, `Award`, `Fandom` 같은 typed
entity에만 허용한다.

Entity 처리는 다음 두 층으로 나눈다.

- 결정적 consolidation: 확실한 정규화·기존 노드 후보 연결
- agent proposal: 타입·관계 제안 후 provenance validator 통과 시 저위험 타입만 자동 승격

자동 승격은 supporting Fact의 `source_excerpt`가 있고 작품/수집 스코프가 구조적으로
확인될 때만 허용한다.

### 2.3 그래프 탐색

보고서용 탐색은 그래프 적재가 확정된 뒤 읽기 전용으로 수행한다.

현재 방식:

- Work 앵커에서 축별 edge prior를 사용하는 멀티홉 walk
- Work 스코프 안의 Fact/WebDoc 벡터 검색
- 후보를 LLM reranker가 대상 일치, 축 관련성, 출처 품질, 중복 기준으로 선별

필수 안전장치:

- 공유 Artist/Platform/Award를 통과해 다른 Work로 넘어가지 않도록 hub-as-leaf 적용
- 이전 Report/Claim과 미해소 Entity는 외부 근거 후보에서 제외
- Query 자체는 조사 provenance이지 사실 근거가 아니므로 최종 evidence에서 제외
- 아티스트 맥락 검색도 Work가 생성한 Query corpus에 포함하되 `evidence_type`으로 작품
  직접 근거와 구분

남은 설계 과제:

- snapshot/version ID를 Query, Fact, Report에 공통으로 부여
- 시간에 따라 변하는 metric의 `observed_at`과 장기 추이를 별도 모델링
- source_excerpt와 Fact 사이의 의미적 함의 검사

### 2.4 보고서 생성

입력:

- 축별 선별 Evidence와 발견 경로
- 평가 대상 브리핑
- STORM outline

출력:

- 사람이 읽는 Markdown
- 구조화 JSON
- `Report → ReportSection → Claim → CITES` 서브그래프

기계 QA:

- factual claim의 citation 존재와 범위
- opinion의 `derived_from`
- 모든 Claim이 Block에 연결됐는지 확인
- block/claim index 범위
- 허용 등급 값
- 모든 축의 `axis_scores` 정확히 1건
- Report graph 적재 성공/실패 기록

ReportSection과 Claim ID는 report ID를 포함해 실행별로 격리한다. 동일한 문장이 여러
보고서에 등장해도 소유권과 버전이 섞이면 안 된다.

## 3. 이번 점검에서 발견한 문제

| 심각도 | 문제 | 영향 | 처리 |
|---|---|---|---|
| P0 | LLM 질의를 validator가 템플릿으로 교체 | LLM 자율성이 사라지고 질의가 반복됨 | 수정 |
| P0 | Fact가 대상명 일치만으로 grounding 통과 | 문서에 없는 사회적 해석이 Fact로 적재됨 | `source_excerpt` 필수화 |
| P0 | Query와 Fact의 직접 provenance 부재 | 축별 coverage와 QA에서 Fact가 섞임 | `EXTRACTED_BY` 추가 |
| P0 | Artist 대상 수집물이 Work 스코프에서 차단 | 수집했지만 보고서 탐색에서 사용 불가 | Work에도 Query 연결 |
| P1 | 질의 outcome이 `run_expand` 호출 사이에 소실 | STORM 다음 턴이 실패 질의를 반복 | Query 노드에 영속화 |
| P1 | novelty가 DB 전체 및 최초 기준과 비교 | 다른 작품/이전 라운드가 중단 판단 오염 | Work-local, 라운드별 갱신 |
| P1 | MERGE도 모두 신규 적재로 집계 | novelty와 query utility가 부풀려짐 | 실제 create 여부 반환 |
| P1 | 축 coverage가 Work/Artist의 모든 Fact를 복제 집계 | 모든 축이 충분해 보이는 허위 신호 | axis Query provenance 기준으로 변경 |
| P1 | STORM 전체 query budget 부재 | 검색·LLM 비용 폭증 | 공유 budget 추가 |
| P1 | ReportSection/Claim ID가 보고서 간 충돌 | 과거 보고서 그래프가 덮어써짐 | report별 namespace |
| P1 | 보고서 기계 QA가 citation 번호만 검사 | block/claim 누락과 축 등급 누락 통과 | 구조 QA 보강 |
| P2 | 범용 검색 결과의 출처 권위 검증이 약함 | TikTok discover 수치 등 의미가 모호함 | 남음 |
| P2 | Entity 승격 성공률이 낮고 review만 누적 | 그래프가 Fact 중심 RAG에 머무름 | 부분 개선, 재검증 필요 |
| P2 | 문서 snippet만 사용 | 본문 문맥·표·날짜를 놓칠 수 있음 | 남음 |
| P2 | LLM reranker가 entailment verifier 역할까지 겸함 | 과대해석을 완전히 차단하지 못함 | 남음 |
| P2 | Tavily quota 소진 후 반복 호출 | 불필요한 지연·오류 로그 | 프로세스 circuit breaker 추가 |

## 4. 2026-06-29 E2E 결과와 해석

실행:

```text
우예린 - 토끼
workflow=LangGraph
curation=discourse(STORM)
기존 그래프에 증분 실행
```

수집 결과:

- 검색 47회
- 선택 문서 237건
- 적재 문서 133건
- grounded로 판정된 Fact 306건
- 당시 신규로 집계된 Fact 252건
- 평균 query utility 0.546
- utility 0인 질의 8건
- 문서 폐기: 대상 아티스트 없음 74건, 저관련/빈 결과 30건
- 전체 토큰 약 479,925

개선 확인:

- 질의가 차트/수상 템플릿 반복에서 비평, 가사, 세대 전이, 공식·검증, 플랫폼 metric,
  공적 활용 등 구조화된 조사 과제로 확장됐다.
- query별 channel/strategy/outcome이 trace에 남았다.
- 보고서별 section/claim ID와 report graph audit가 정상 기록됐다.

실패 확인:

- Genius 가사 페이지에서 직접 확인되지 않는 `청소년 정체성`, `청소년 공감` 해석이
  Fact로 생성됐다.
- 이 Fact를 근거로 `SocialTheme(청소년 정체성)`가 승격되고 사회적 가치 B-에 반영됐다.
- 따라서 해당 실행의 사회적 가치 결론은 신뢰할 수 없다.
- 이 실패 이후 `source_excerpt`와 `EXTRACTED_BY` 계약을 추가했으므로, 위 실행은 새
  계약의 검증 결과가 아니라 결함 발견용 baseline이다.

새 계약 제한 검증(질의 1개):

- 선택 문서 6건 중 적재 1건
- 4건은 `source_excerpt`/grounding 불충족으로 차단
- 신규 Fact 1건에 `source_excerpt` 저장 확인
- `Fact → EXTRACTED_BY → Query(axis=influence)` 경로 확인
- 축 coverage가 influence 0건에서 1건으로만 증가하고 다른 축은 0건 유지

산출물:

- `music_copyright/reports/e2e_toggi_adaptive_20260629_114115/report.json`
- `music_copyright/reports/e2e_toggi_adaptive_20260629_114115/report.md`
- `music_copyright/reports/rpt_20260629_114120_05cb89c1/trace.jsonl`

## 5. 자율성 경계

높은 자율성:

- 조사 질문 생성
- 검색 전략과 채널 선택
- 실패 outcome을 본 재계획
- 읽기 전용 그래프 탐색과 관점 발견

중간 자율성:

- 문서에서 Fact/mention 제안
- Entity 타입·동일성·관계 제안

낮은 자율성:

- provenance 없는 Fact 커밋
- 고가중 관계 자동 커밋
- 온톨로지 변경
- citation 없는 보고서 주장
- 최종 등급 형식과 축 누락

코드는 의미 판단을 템플릿으로 대신하지 않고, 존재·스코프·provenance·예산·형식 같은
검증 가능한 불변식을 담당한다.

## 6. 다음 검증 순서

1. 음악 그래프를 비우고 새 provenance 계약으로 `토끼`를 처음부터 실행한다.
2. 모든 웹 Fact에 `source_excerpt`와 `EXTRACTED_BY`가 있는지 검사한다.
3. `청소년 정체성`처럼 원문에 없는 해석이 0건인지 확인한다.
4. STORM 2번째 턴의 Planner 입력에 이전 query outcome이 포함되는지 확인한다.
5. 전체 검색이 공유 budget 24 내에 머무는지 확인한다.
6. Entity 승격별 supporting excerpt와 Work/collection anchor를 검사한다.
7. 축별 retrieval evidence의 직접성·출처 독립성·시간 기준을 검사한다.
8. Claim → Evidence → Fact → WebDoc → Query → ResearchTask 체인을 역추적한다.
9. 동일 절차로 `Dynamite`를 실행해 희소 작품과 대형 작품 양쪽의 일반화를 비교한다.

기존 그래프에는 새 계약 이전 Fact와 잘못 승격된 SocialTheme가 남아 있다. 깨끗한 재실행
전에는 새 코드의 보고서 품질을 기존 그래프 위에서 판정하면 안 된다.
