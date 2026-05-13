# 00. Overview

## 한 줄

STO(Security Token Offering, 토큰증권) **기초자산의 감정평가**를 LLM 온톨로지로
정형화하는 파이프라인.

## 문제

기존 조각투자/토큰증권 시장에서 **발행가와 시장가의 괴리**가 크다. 가설:
이 괴리는 **사회적·비정형·맥락적 가치**(시장 심리, 정책 변화, 임차인 업황,
지역 호악재)가 평가 과정에서 누락되기 때문이다.

## 처방

비정형 텍스트(뉴스·정책·이벤트)를 **온톨로지**로 태깅 → **지식 그래프**에
적재 → 자산 평가 시 그래프 traversal 로 **evidence 기반 평가 리포트** 생성.

## 평가 3축

| 축 | 의미 | 현재 구현 |
|---|---|---|
| 정량 (Quantitative) | 수익률·매출·거래량 등 수치 | 본 프로젝트 범위 밖 (`score=0.0` stub) |
| 구조 (Structural) | 법적 안정성·권리관계·토큰 구조 | 본 프로젝트 범위 밖 (`score=0.0` stub) |
| **감정 (Sentiment)** | 사회적·맥락적 신호 | **본 프로젝트 핵심** |

자산별로 `EvaluationFactor` 사전이 다르다. 상가 부동산은
`임대수익률 / 공실률 / 거래량 / 상권 활성도 / 금리 민감도 …`, 음악 저작권은
`스트리밍 추세 / 정산 …`. 자산별 사전은 `projects/<자산>/domain/factors.py` 에.

## 두 실행 흐름

### A. 배치 흐름 — 자동 뉴스 수집

```
search_groups.yaml → NaverNewsClient → NewsNormalizer → NewsRepository
  → RuleFilter → Embedding → SemanticDedup → OntologyTagger → GraphSignalWriter
```

매일 실행 (Airflow `@daily`). 결과: `news_articles`, `ontology_tags`,
`market_signals`, `graph_nodes/edges` 누적.

### B. 온디맨드 흐름 — 주소 분석 (멀티에이전트)

```
주소 입력
  → InitialCollector (JUSO → VWorld → MOLIT → 건축물대장)
  → ExplorationFlow (Planner → PolicyGate → Retriever → Critic, max_iter=3)
  → OntologyMapper / GraphStore.upsert (scope_id=analysis_<uuid>)
  → ReportBuilder
  → QAFlow (Analyzer → Retriever → Synthesizer → Verifier, max_retries=2)
```

결과: `AnalysisResult { claims, evidence_trace, report_text }`.

## 두 흐름이 만나는 지점

**공유 온톨로지 어휘**를 통해 자동 연결.

```
배치:    [News] -GENERATES-> [Signal] -AFFECTS-> [EvaluationFactor: "공실률"]
온디맨드: [Address] -HAS-> [Building: PNU] -AFFECTED_BY-> [EvaluationFactor: "공실률"] ← 공유
```

같은 `EvaluationFactor` 노드를 traversal 함으로써 "이 빌딩의 공실률 평가에
영향을 주는 최근 시장 신호"가 자연스럽게 연결된다.

## POC 자산

- **상가형 부동산** — MVP 실 구현 대상. `BuildingTarget(PNU)`, `TenantTarget(사업자번호)`.
- **음악 저작권** — placeholder. `SongTarget(ISRC)`, `ArtistTarget(name+agency)`.

자세한 비즈니스 사양은 [`product.md`](product.md).
