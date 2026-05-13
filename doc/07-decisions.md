# 07. Decisions (ADR)

Architecture Decision Records — 우리가 굳힌 설계 결정과 *왜*. 새 결정이
이전 결정을 뒤집을 때, 옛 ADR 을 **지우지 말고 `Status: Superseded by N`**
으로 표기하고 새 ADR 을 추가한다. 이유를 잃지 않기 위함.

---

## ADR-001 · 최상단 3분기: `common/` / `projects/` / `airflow/`

**Status**: Accepted

**Context**: 자산-불문 공통 코드와 자산별 코드의 경계, 그리고 배치 스케줄러
경계가 명확히 분리되어야 함.

**Decision**:
- 최상단을 `common/` (공통 인프라 + 최소 추상), `projects/` (자산별 구체화),
  `airflow/` (DAG + 진입 스크립트) 로 나눈다.
- src layout (`src/sto_valuation/...`) 은 사용하지 않는다 — 단일 앱이라 import
  경로만 길어짐.

**Consequences**:
- `common` 은 `projects` 를 절대 import 하지 않는다.
- 새 자산 추가 시 `common` 무수정.
- 다중 자산 cross import 도 금지.

---

## ADR-002 · 폴더명 `projects` (복수)

**Status**: Accepted

**Context**: 안에 여러 자산이 들어가므로 의미적으로 복수가 정확.

**Decision**: `projects/` 사용. (단수 `project/` 안 함)

---

## ADR-003 · 4계층 외부 연결: clients / services / tools / mcp

**Status**: Accepted

**Context**: 외부 API 호출과 LLM 노출이 한 클래스에 섞이면 책임이 흐려진다.

**Decision**:
| 계층 | 책임 |
|---|---|
| clients | 외부 1회 호출. 인증·재시도·rate-limit·로깅 외 로직 없음 |
| services | clients 합성, 정규화, 필터, 도메인 변환 |
| tools | services 의 LLM 래퍼. 비즈니스 로직 0 |
| mcp | tools 의 외부 프로세스 노출 |

**Consequences**:
- raw 응답 저장용 폴더 없음. service 가 즉시 domain 으로 변환.
- clients 는 domain 을 import 하지 않음 (ACL).
- tools 는 항상 1 service 위에서만 산다.

---

## ADR-004 · 자산 domain 은 `projects/<자산>/domain/` 에

**Status**: Accepted

**Context**: 음악 저작권을 깊게 조사하지 않아, 부동산에서 도출한 도메인 모델이
음악에 그대로 맞을지 불확실. 잘못된 추상화는 비용이 크다.

**Decision**: `common/domain` 에는 자산-불문이라 확신되는 최소만
(AssetClass, AssetProfile, Target ABC, GraphNode/Edge, RelationType). 그 외는
전부 `projects/<자산>/domain/`. 두 번째 자산이 사실로 강제할 때 retro-fit.

**Consequences**:
- `EvaluationFactor`, `OntologyTag`, `MarketSignal`, `Evidence`, `EvaluationClaim`,
  `AnalysisResult` 는 자산별.
- 그래프 적재 일관성은 자산이 표준 키 (snake_case factor key) 를 지키는 것으로 유지.

---

## ADR-005 · Raw 응답 모델 별도 폴더 미사용

**Status**: Accepted

**Context**: 외부 응답을 처음 어디에 둘지 (별도 `_schemas/` vs client 옆 vs 안 둠).

**Decision**: 별도 폴더 없음. client 의 출력은 dict 또는 client 파일 안의
가벼운 pydantic 모델. service 가 즉시 domain model 로 변환하고 raw 는 GC.

**Consequences**:
- 진단 목적의 raw 저장이 필요해지면 별도 storage adapter 로 분리 (도메인 미오염).

---

## ADR-006 · 상속 vs 합성 분기

**Status**: Accepted

**Context**: "core 도구를 자산이 상속" 표현이 두 가지로 해석 가능 — 진짜 상속 vs
객체 합성.

**Decision**:
- **상속**: 공통 인프라. `BaseClient`, `BaseTool`, `BaseAgent`, `BaseLLM`, `BaseMCPServer`
- **합성**: 비즈니스 조립. services, flows (clients/llm/repos 주입)

`Service(Client)` 식의 상속은 금지 — 한 service 가 여러 client 를 사용하므로
표현 불가, fake 주입 어려움.

---

## ADR-007 · Pure function first

**Status**: Accepted

**Context**: OO 와 함수형의 혼합 스타일 요청.

**Decision**: 의존성이 없으면 모듈 함수, 있으면 클래스로 승격. 클래스 메서드는
pure 함수 합성의 얇은 오케스트레이터 (Functional Core, Imperative Shell).
폴더 구조에는 영향 없음 — 파일 안의 규칙.

---

## ADR-008 · 옵저버빌리티는 cross-cutting + 베이스 자동 hook

**Status**: Accepted

**Context**: Langfuse / structlog / cost / quota 를 1급 시민으로.

**Decision**:
- `common/observability/` 로 분리. `BaseLLM`, `BaseClient`, `BaseTool`, `BaseAgent`
  안에서만 호출 — 자산 코드는 옵저버빌리티 API 를 직접 부르지 않음.
- Langfuse 의존은 **선택적**. env 키 없으면 no-op.
- 비용·쿼터 store 는 in-memory. ports 만 정의해 추후 redis 등 교체 가능.

---

## ADR-009 · 멀티 LLM, 역할별 매핑은 자산 config YAML

**Status**: Accepted

**Context**: planner/retriever/critic 등이 서로 다른 모델을 쓸 수 있어야 함.

**Decision**:
- 1차 어댑터: **Anthropic + OpenAI**.
- 역할별 매핑은 `projects/<자산>/config/llm.yaml` (진실 소스).
- `LLM_<ROLE>` env 가 있으면 YAML 보다 우선.
- `RouterLLM` (task hint 기반 분기) 는 stub 만 — 실 라우팅은 필요해질 때.

---

## ADR-010 · DI 는 수작업 함수, 라이브러리 미사용

**Status**: Accepted

**Context**: `dependency-injector`/`punq` 같은 라이브러리도 있음.

**Decision**: `composition.py` 안 `build_<asset>(settings) -> <Asset>Context`
함수로 수작업 wiring. 자산 수가 10 미만인 한 라이브러리보다 명시적·디버깅하기
쉬움·타입 안전.

**Re-evaluate when**: 자산 수가 늘어 함수가 너무 길어지면 자산별
`projects/<x>/_wiring.py` 로 분리.

---

## ADR-011 · 어댑터 위치는 평탄 (`adapters/` 하위 없음)

**Status**: Accepted

**Context**: `common/clients/adapters/naver_news.py` 처럼 한 단계 더 들어갈지.

**Decision**: 평탄. `common/clients/{base.py, naver_news.py, web_search.py}`.
폴더 자체가 책임 격리 단위이므로 한 번 더 `adapters/` 는 깊이만 늘림.

---

## ADR-012 · ports 인터페이스는 `common/ports/` 한 곳에 모음

**Status**: Accepted

**Context**: 자산이 구현해야 할 protocol 들(OntologyTagger, GraphSignalWriter)을
어디 둘지.

**Decision**: 모든 외부 의존 인터페이스는 `common/ports/`. 자산은 "내가 구현해야
할 ports" 카탈로그를 한 곳에서 본다. 서비스 로컬 `protocols.py` 산재 X.

---

## ADR-013 · Repository / Graph / Embedding 어댑터는 책임별 폴더

**Status**: Accepted

**Context**: in-memory 구현체를 어디에 둘지 (`common/adapters/...` vs 분리).

**Decision**: 책임별 폴더. `common/repositories/`, `common/graph/`,
`common/embedding/` 각각 `base.py + memory.py`. 거대 `adapters/` 폴더 미사용
(LLM 어댑터는 `common/llm/` 안에 있는데 storage 만 다른 폴더 → 일관성 깨짐).

---

## ADR-014 · 자산-종속 주입 인터페이스 = `common/ports/news.py`

**Status**: Accepted

**Context**: 뉴스 pipeline 은 자산의 `OntologyTagger`/`GraphSignalWriter` 를
주입받음. 이 인터페이스를 로컬에 둘지 ports 에 둘지.

**Decision**: `common/ports/news.py`. ports = 인터페이스 카탈로그라는 일관성.

---

## ADR-015 · 테스트 폴더는 src 1:1 미러 + `tests/smoke/` 별도

**Status**: Accepted

**Decision**:
- `tests/common/<...>` 가 `common/<...>` 미러.
- `tests/projects/<자산>/<...>` 가 `projects/<자산>/<...>` 미러.
- `tests/smoke/test_imports.py` 가 의존 그래프 + composition wire 검증.

---

## ADR-016 · MCP 서버 = 공통 + 자산별 분리 (양쪽 stub)

**Status**: Accepted, Stub-only

**Context**: 에이전트가 자산 도구를 어떻게 받을지 (자산별 MCP / 공통 MCP / 둘 다 / 안 씀).

**Decision**: 공통 도구는 `common/mcp/` 에, 자산 전용은 `projects/<자산>/mcp/` 에.
지금은 stub 만 두고 실 활성화는 추후. 외부 MCP 서버를 *사용*하는 길도 열려 있음
(`common/mcp/client.py`).

---

## ADR-017 · `common/config/settings.py` 의 pydantic-settings 는 lazy import

**Status**: Accepted

**Context**: skeleton 단계에서 dev deps 미설치 환경 (`ModuleNotFoundError:
pydantic_settings`) 으로 import 가 깨지면 안 됨.

**Decision**: `Settings` 클래스를 `get_settings()` 안에서 정의·인스턴스화.
모듈 import 자체는 외부 deps 무 의존.
