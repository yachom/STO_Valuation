# 03. Coding Conventions

## Pure Function First

> 의존성이 없으면 **모듈 함수**. 있으면 **클래스로 승격**. 클래스 메서드는
> pure 함수 합성의 얇은 오케스트레이터.

이 규칙은 Gary Bernhardt 의 **Functional Core, Imperative Shell** 을 따른다.
폴더 구조에 영향을 주지 않는다 — 파일 안의 작성 규칙이다.

### Pure 함수로 쓰는 경우

- 입력 → 출력 변환만 하는 것
- 외부 호출(HTTP, DB, LLM)이 없는 것
- 시간/난수 같은 hidden state 가 없는 것

예:

```python
# common/services/news/normalizer.py
def normalize_html(html: str) -> str: ...
def normalize_item(raw: dict, *, source: str) -> NormalizedNews: ...

# common/services/news/dedup.py
def cosine(a: list[float], b: list[float]) -> float: ...
def is_duplicate(emb, existing, threshold=0.92) -> bool: ...

# common/utils/__init__.py
def sha256_hex(text: str) -> str: ...
def normalize_url(url: str) -> str: ...
```

### 클래스로 승격하는 경우

- 외부 SDK 의 lifecycle (httpx Client, LLM provider)이 있음
- 의존성 주입(clients, repos, llm)이 필요함
- 캐시/상태(token usage 누적 등)를 들고 있음

예:

```python
class InitialCollectorService:
    def __init__(self, juso, vworld, molit, ledger): ...
    def collect(self, address: str) -> AddressMetadata: ...
```

`InitialCollectorService.collect()` 는 내부에서 `_resolve_address(...)`,
`_lookup_pnu(...)` 같은 **pure 함수**를 호출하는 얇은 오케스트레이터.

## 상속 vs 합성

| 사용 | 예 |
|---|---|
| **상속** — 공통 인프라 | `BaseClient` ← `JusoClient`, `BaseLLM` ← `AnthropicLLM`, `BaseAgent` ← `CommercialPlanner`, `BaseTool` ← `*Tool`, `BaseMCPServer` ← `CommercialMCPServer` |
| **합성** — 비즈니스 조립 | `InitialCollectorService(juso=..., vworld=..., molit=..., ledger=...)`, `AnalyzeAddressFlow(initial, planner, retriever, critic, report_builder)` |

이유:
- 베이스 클래스 = "공통 *행동*". 자산은 부분 오버라이드 + 새 메서드.
- 서비스/플로우 = "여러 객체를 *합쳐* 만든 결과". 다중 client 를 표현하려면
  상속(특히 다중 상속)은 부적합. fake 주입으로 테스트하기도 합성이 단순.

**경고:** `Service(Client)` 식의 상속은 금지. service 와 client 는 책임이 다르고
service 는 여러 client 를 다룬다.

## 명명 규칙

| 종류 | 패턴 | 예 |
|---|---|---|
| Pure 함수 | `동사_명사` | `normalize_html`, `compute_url_hash`, `score_signal`, `apply` |
| 클래스 | `명사+역할` | `JusoClient`, `InitialCollectorService`, `CommercialPlanner` |
| 클래스 메서드 | 짧은 동사 | `.run()`, `.collect()`, `.search()`, `.tag()` |
| 상수 | `UPPER_SNAKE` | `COMMERCIAL_EVALUATION_FACTORS`, `LOCATED_IN` |
| 파일/모듈 | `snake_case` | `initial_collector.py`, `graph_signal.py` |

## 파일 구조 안의 순서

각 모듈은 위에서 아래로 다음 순서:

```python
"""모듈 docstring — '무엇'이 아니라 '왜'."""

from __future__ import annotations

# 1. stdlib
# 2. third-party
# 3. project (common → projects 순)

# 4. 모듈 레벨 상수 / Enum
# 5. pydantic 모델 / dataclass (작은 것 먼저)
# 6. 모듈 레벨 pure 함수
# 7. 클래스 (베이스 → 구상 순)
```

## 타입

- Python 3.11+. `from __future__ import annotations` 모든 파일에.
- pydantic v2 `BaseModel` (도메인 모델, 응답 모델).
- protocol = `typing.Protocol` + `@runtime_checkable` (ports 만).
- ABC = `abc.ABC` (Target 같은 도메인 추상).
- 함수 시그니처는 항상 타입 명시. `Any` 는 마지막 수단.

## 주석

**기본: 주석을 쓰지 않는다.** 잘 지어진 식별자가 *무엇*을 설명한다.
주석은 다음 경우에만:

- 숨은 제약 / 불변식
- 코드만으론 추측 못 할 *왜* (예: 외부 API 의 비공식 동작 회피)
- 일시적 워크어라운드 + 이슈 링크

금지:
- "이 코드는 X 를 한다" (코드를 한국어로 옮기는 주석)
- "현재 작업 / 이슈 #N 을 위해 추가됨" — PR 설명에 적고, 코드에는 두지 않음

## 함수/메서드 본문

뼈대 단계: 모든 본문은 `raise NotImplementedError` 또는 `...`.
**시그니처는 정확히**, 본문은 비어 있어도 됨. 한 번에 한 함수씩 구현으로 채운다.

## 에러 처리

- 도메인 예외는 `common/errors.py` 의 `STOError` 계층 사용.
- 외부 API 실패 = `ExternalAPIError` (또는 더 구체적인 서브클래스).
- LLM 호출 실패 = `LLMError`.
- 내부 코드의 broad `try / except Exception` 은 금지. 정확한 예외만 잡는다.
- 보일러플레이트 retry/timeout 은 `common/clients/base.py` 와 `common/llm/base.py`
  의 베이스가 담당. 구상 어댑터는 본인 로직만 작성.

## 검증 vs 신뢰

- **시스템 경계(외부 API 응답, 사용자 입력, 환경 변수)** 에서만 validate.
- 내부 호출에서 같은 데이터를 다시 검증하지 않는다. pydantic 모델이 곧 계약.

## 테스트

- pure 함수 = 인자/반환으로만 검사 (mock 0).
- 서비스/에이전트 = 합성된 의존성을 fake 로 주입.
- `tests/` 는 `src/` 1:1 미러 + `tests/smoke/` 별도.
- smoke 테스트는 import 가능성 + composition wire 검증.
