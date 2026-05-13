# 문서 인덱스

이 폴더는 프로젝트의 **설계 사실들**을 모은다. 코드와 항상 동기화되어야 하는
문서는 폴더 트리 같은 *현재 상태*가 아니라 **결정·계약·규칙**이다.

## 읽는 순서

| # | 문서 | 무엇이 들어 있나 |
|---|---|---|
| 00 | [overview.md](00-overview.md) | 프로젝트 목적·문제·평가 3축·두 실행 흐름 (한 페이지) |
| 01 | [architecture.md](01-architecture.md) | 폴더 구조, 의존성 그래프, 4계층 책임 분리 |
| 02 | [domain-model.md](02-domain-model.md) | `common/domain` vs `projects/<자산>/domain` 분기 원칙 |
| 03 | [coding-conventions.md](03-coding-conventions.md) | Pure function first, 상속/합성, 명명 규칙 |
| 04 | [observability.md](04-observability.md) | Langfuse / structlog / cost / quota 통합 지점 |
| 05 | [llm-multi-provider.md](05-llm-multi-provider.md) | LLM 어댑터, 역할별 모델 매핑, RouterLLM |
| 06 | [extension-guide.md](06-extension-guide.md) | 새 자산을 추가하는 단계별 절차 |
| 07 | [decisions.md](07-decisions.md) | ADR — 우리가 합의해 굳힌 설계 결정들 |
| 08 | [open-questions.md](08-open-questions.md) | 아직 미결, 추후 토론할 것들 |

## 별도 산출물

- [product.md](product.md) — 비즈니스 사양 (원본). 본 문서들은 이 사양을 코드화한
  설계 결정의 기록.
- `.claude/plans/5-lazy-turing.md` — 뼈대 작업 완료 시점의 plan 파일 스냅샷.

## 문서 유지 규칙

1. **결정이 바뀌면 `07-decisions.md` 의 해당 ADR 을 deprecate 하고 새 ADR 을 추가**한다. 과거 결정을 지우지 않는다 — 이유를 잃는다.
2. 폴더 트리·파일명은 `01-architecture.md` 에 단 한 번만 기록한다. 다른 문서는 *책임*과 *경계*만 다룬다.
3. 외부 문서(API 스펙, 정부 데이터 매뉴얼 등)는 링크만 두고 본문에 베끼지 않는다.
