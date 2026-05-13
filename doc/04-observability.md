# 04. Observability

## 원칙

옵저버빌리티/비용추적/쿼터는 **1급 시민**. cross-cutting 모듈로 두되,
*자산 코드는 옵저버빌리티 API 를 직접 호출하지 않는다*. 베이스 클래스만
호출하고, 자산은 베이스를 상속하면 자동으로 trace/cost/quota 가 따라온다.

## 모듈

| 파일 | 책임 |
|---|---|
| `common/observability/logging.py` | structlog 기반 구조화 로깅 설정. `configure_logging()` 을 app entry 에서 1회 호출. |
| `common/observability/langfuse.py` | Langfuse SDK 래퍼. **선택적 의존**: env 키 없으면 모든 함수가 no-op. |
| `common/observability/tracing.py` | provider-agnostic `@traced` 데코레이터, `span()` 컨텍스트. |
| `common/observability/context.py` | `correlation_id`, `scope_id` ContextVar. |
| `common/observability/cost.py` | 요청 scope 단위 LLM/API USD 누적. PolicyGate 가 읽는다. |
| `common/observability/quota.py` | 외부 API 일별 호출 카운터 (예: VWorld 무료 한도). |

## 통합 지점 (베이스 클래스가 자동 처리)

| 위치 | 자동 hook |
|---|---|
| `common/llm/base.py` `BaseLLM.generate()` | Langfuse `generation` 기록 + token 비용을 `cost.add(...)` 에 누적 |
| `common/clients/base.py` `BaseClient._request()` | 호출 로그 (latency·status·endpoint) + `quota.increment(api)` |
| `common/tools/base.py` `BaseTool.run()` | `@traced` span (입출력 캡처) |
| `common/agents/base_agent.py` `BaseAgent.run()` | root 또는 child trace 시작, correlation_id 부여 |
| `common/flows/exploration.py`, `qa.py` | LangGraph 노드 enter/exit 마다 span |
| `common/services/news/pipeline.py` | per-stage span; root trace=DAG run_id |
| `common/agents/policy.py` `PolicyGate.check()` | `cost.get(scope)` 로 `max_cost_usd` 검사 |

자산 코드는 위 베이스만 사용하면 옵저버빌리티가 *공짜*.

## scope_id 사용

온디맨드 요청 1건이 시작될 때 `scope_id = "analysis_<uuid>"` 를 부여.
모든 cost·graph 적재는 이 scope 로 격리된다. 동시 요청이 서로의 비용 카운터·
그래프 노드를 오염시키지 않게 하기 위함.

```
cli.analyze-address
  → composition.resolve("commercial")
  → context.set_scope("analysis_" + uuid4().hex)
  → flow.run(address)
      → 모든 LLM/API 호출이 같은 scope 에서 누적
  → context.clear()
```

배치 흐름은 `scope_id = "batch_<date>"` 처럼 날짜 기반.

## Langfuse 의존의 선택성

- 환경 변수 `LANGFUSE_PUBLIC_KEY` 또는 `LANGFUSE_SECRET_KEY` 가 비어 있으면
  `langfuse.py` 의 모든 함수가 no-op (no-op span/trace).
- CI/로컬 개발자는 langfuse 계정 없이도 전 코드 실행 가능.
- production 에서만 활성화.

## 비용 store 의 미래 교체

`cost.py` 는 1차로 in-memory dict (process-local). 확장 시:

- 분산 환경(여러 워커가 같은 scope 를 다룸) 이면 redis 어댑터로 교체.
- 그때 `common/ports/` 에 `CostStore` 인터페이스를 추가하고 cost.py 가 그 인터페이스
  뒤로 숨도록 리팩터.

지금은 도입하지 않음 (premature abstraction 회피).

## 쿼터 store 의 미래 교체

`quota.py` 도 동일. 1차는 in-memory. 외부 API quota 가 일일 한도라 분산 환경
없이는 redis 가 필요 없다.

## 로그 출력 규약

- key=value 구조 (structlog 기본).
- PII 미포함. 주소/이름은 hash 또는 short id 로.
- 외부 API 응답 본문은 로그에 남기지 않음 (PII + 크기). 메타데이터(status, latency, request_id)만.

## 다음 확장 (후순위)

- OpenTelemetry export (langfuse 와 병행 가능)
- Prometheus 메트릭 (요청 카운터, latency 히스토그램)
- 알람: `cost.get(scope) > threshold` 또는 `quota.get(api) > 80% of limit`
