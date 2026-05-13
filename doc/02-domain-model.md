# 02. Domain Model

## 원칙

> **자산-불문이라고 확신되는 최소만 `common/domain` 에. 나머지는 전부
> `projects/<자산>/domain` 에.**

이유: 음악 저작권 자산을 아직 깊게 조사하지 않아, 부동산에서 도출한 모델이
음악에 그대로 맞을지 불확실하다. 부동산에서 한 번, 음악에서 한 번 만들어본 뒤
**두 번째 자산이 등장할 때 공통화(retro-fit)** 한다. 잘못된 추상화는
삭제 비용이 추상화 자체보다 비싸다.

## `common/domain` 에 두는 것 (자산-불문)

| 모델 | 위치 | 이유 |
|---|---|---|
| `AssetClass` (enum) | `asset.py` | CLI/레지스트리/오케스트레이션 공용 식별자 |
| `AssetProfile` | `asset.py` | 자산 메타·config_dir — `composition.py` 가 dispatch |
| `Target` (ABC) | `target.py` | "평가 단위" 추상. `.identifier` 만 강제 |
| `GraphNode` / `GraphEdge` | `graph.py` | 그래프 **자료구조**는 자산 불문 |
| `RelationType` (str 베이스) | `graph.py` | 자산이 자기 라벨을 plain str 로 추가 가능 |

## `projects/<자산>/domain` 에 두는 것 (자산별)

| 모델 | commercial 위치 | 왜 자산별인가 |
|---|---|---|
| 구상 `Target` (BuildingTarget, TenantTarget) | `targets.py` | 식별자 종류·필드가 자산마다 다름 (PNU vs ISRC) |
| `EvaluationFactor` 사전 | `factors.py` | 공실률 vs 스트리밍 추세 — 사전이 다름 |
| `FactorAxis` enum | `factors.py` | 자산마다 축의 의미가 다를 수 있음 |
| 6축 `OntologyTag` enum | `ontology.py` | 부동산용 축 (Society/Policy/Tenant/Market/Macro/Local). 음악 도입 시 재검증 |
| `SignalType` enum | `signals.py` | 자산 신호 분류 |
| `MarketSignal`, `Evidence` | `signals.py` | tag 가 자산 ontology 에 의존 → 모델도 자산별 |
| `EvaluationClaim`, `AnalysisResult` | `claims.py` | factor_key/axis 가 자산 사전에 의존 |
| 자산 특화 라벨 (`LOCATED_IN`, `LEASED_BY` …) | `relations.py` | 자산이 자기 관계를 정의 |

## 모델 간 관계

```
                AssetProfile          (common)
                     │
                     ▼
   ┌─── projects/commercial/domain ──────────────────────────┐
   │                                                          │
   │   BuildingTarget(pnu)                                    │
   │   TenantTarget(business_number)                          │
   │           │                                              │
   │           │ identifier                                   │
   │           ▼                                              │
   │   AnalysisResult.target_id                               │
   │       └─ claims: list[EvaluationClaim]                   │
   │            │     factor_key ─→ COMMERCIAL_EVALUATION_FACTORS  │
   │            │     axis      ─→ FactorAxis                 │
   │            │     evidence: list[Evidence]                │
   │            └─ evidence_trace: list[Evidence]             │
   │                                                          │
   │   MarketSignal                                           │
   │       ├─ tag: OntologyTag (6축)                          │
   │       └─ evidence: list[Evidence]                        │
   └──────────────────────────────────────────────────────────┘

   GraphNode / GraphEdge (common.domain.graph)
       — 자산 관계없이 동일 자료구조로 적재.
       — labels / properties 에 자산 의미를 담음.
```

## "승격" 절차 — 자산별 → 공통

음악 자산을 실 구현하다 보면 부동산과 모양이 같은 모델이 보일 수 있다.
그때 다음 순서로 공통화한다:

1. 두 자산이 *현재* 사용 중인 코드를 비교 (이름·필드·메서드).
2. 차이가 *진짜* 자산 의미인지(쪼개야 함) *우연*인지(공통화 가능) 판정.
3. 공통 부분만 `common/domain` 으로 끌어올리고, 자산은 그것을 import 하도록 변경.
4. `07-decisions.md` 에 ADR 추가 ("`X` 를 common 으로 승격, 이유: …").

승격 *전*에 추상화하지 않는다. "두 번째 사례가 추상화를 강요할 때만."

## 그래프 적재의 일관성

배치 흐름과 온디맨드 흐름이 **같은 `EvaluationFactor` 노드**에서 만나는 것이
설계의 핵심 (overview §"두 흐름이 만나는 지점" 참고). 그러려면:

- 자산이 자기 `EvaluationFactor` 키를 그래프 노드 ID 로 일관되게 사용해야 한다.
- 자산의 `OntologyTagger` 와 `GraphSignalWriter` 가 같은 키 집합을 알고 있어야 한다.
- 키는 영문 snake_case (`vacancy_rate`, `tenant_stability`) 로 표준.

이 규칙은 `projects/commercial/domain/factors.py` 의 `COMMERCIAL_EVALUATION_FACTORS`
가 진실의 소스 (single source of truth).
