# 05. Multi-LLM & Role-Based Routing

## 목표

- **여러 LLM provider** 동시 지원 (Anthropic, OpenAI; 추후 Google/자체 호스팅).
- **역할별 LLM 매핑** — planner/retriever/critic/tagger/synthesizer 가 각자
  다른 모델을 쓸 수 있어야 함 (비용·품질 최적화).
- **Provider 추가가 1 파일 추가**로 끝나야 함.
- **자산 코드는 provider 를 모름** — `LLMClient` 인터페이스만 봄.

## 아키텍처

```
┌─────────────────────────────────────────────────┐
│ projects/<asset>/config/llm.yaml                │ ← 역할 → 모델 매핑 (진실 소스)
└──────────────────────┬──────────────────────────┘
                       │ load_registry(settings, path)
                       ▼
┌─────────────────────────────────────────────────┐
│ LLMRegistry  (common/llm/registry.py)           │
│   role "planner" → AnthropicLLM(claude-opus-4-7)│
│   role "tagger"  → OpenAILLM(gpt-4o-mini)       │
│   …                                             │
└──────────────────────┬──────────────────────────┘
                       │ registry["planner"]
                       ▼
┌─────────────────────────────────────────────────┐
│ AnthropicLLM / OpenAILLM  (extends BaseLLM)     │
│   - retry / timeout                             │
│   - Langfuse generation                         │
│   - cost.add()                                  │
│   - response cache                              │
└──────────────────────┬──────────────────────────┘
                       ▼
                  provider SDK
```

## YAML 형식

`projects/commercial/config/llm.yaml`:

```yaml
planner:
  provider: anthropic
  model: claude-opus-4-7
  temperature: 0.1

retriever:
  provider: openai
  model: gpt-4o-mini
  temperature: 0.0

critic:
  provider: anthropic
  model: claude-sonnet-4-6
  temperature: 0.0

tagger:
  provider: openai
  model: gpt-4o-mini
  temperature: 0.0

synthesizer:
  provider: anthropic
  model: claude-sonnet-4-6
  temperature: 0.2
```

## env var override

`LLM_<ROLE>` (대문자) 가 설정되면 YAML 보다 우선.

```bash
export LLM_PLANNER=claude-sonnet-4-6   # 비용 절감용 운영 중 교체
```

이 우선순위는 12-factor 원칙: 기본은 코드/YAML, 운영 변경은 env.

## DI 흐름

`composition.py` 의 `build_commercial(settings)`:

```python
registry = load_registry(settings, "projects/commercial/config/llm.yaml")
planner   = CommercialPlanner(llm=registry["planner"], tools=[...])
retriever = CommercialRetriever(llm=registry["retriever"], tools=[...])
critic    = CommercialCritic(llm=registry["critic"], tools=[...])
tagger    = CommercialOntologyTagger(llm=registry["tagger"])
```

각 컴포넌트는 단일 `LLMClient` 만 봄. 어떤 provider 인지 모름.

## RouterLLM 패턴 (stub 단계)

한 컴포넌트 안에서 **task hint 별로 모델을 가르고 싶을 때** 사용.
`RouterLLM` 자체가 `LLMClient` 를 구현하므로 다른 LLM 자리에 그대로 주입 가능.

```python
class RouterLLM(BaseLLM):
    def __init__(self, routes: dict[str, LLMClient], default: LLMClient): ...
    def pick(self, task_hint: str | None) -> LLMClient: ...
    def generate(self, messages, *, task_hint=None, ...) -> LLMResponse: ...
```

예 — Planner 안에서 "간단 분류" vs "복잡 추론":

```python
fast_llm = OpenAILLM("gpt-4o-mini")
deep_llm = AnthropicLLM("claude-opus-4-7")
router = RouterLLM({"classify": fast_llm, "reason": deep_llm}, default=deep_llm)
planner = CommercialPlanner(llm=router, ...)
```

지금은 stub. 실 라우팅 로직은 한 자산이 *진짜로 필요할 때* 채운다.

## Provider 추가 절차

새 provider X 를 더할 때:

1. `common/llm/x.py` 작성. `class XLLM(BaseLLM)`. `_call_provider(...)` 구현.
2. `capabilities` 셋 정의 (TOOL_CALLING / JSON_MODE / STREAMING).
3. `cost_per_1k_input/output` 또는 `_compute_cost()` 채움.
4. `common/llm/registry.py` 의 provider→class 매핑 dict 에 `"x": XLLM` 추가.
5. YAML 또는 env 에서 `provider: x` 로 선택 가능.

`common/`/`projects/` 의 다른 파일은 무수정.

## Capability 시스템

provider 마다 API surface 가 다름 — tool calling 포맷, JSON 모드, streaming 등.
`LLMClient.capabilities: set[Capability]` 로 표현.

```python
class Capability(str, Enum):
    TOOL_CALLING = "tool_calling"
    JSON_MODE = "json_mode"
    STREAMING = "streaming"
    VISION = "vision"
```

에이전트는 호출 전 `if Capability.JSON_MODE in llm.capabilities: …` 식 가드.
지원 안 되면 fallback (regex parse 등).

## 비용·트레이스

모든 LLM 호출은 `BaseLLM.generate()` 경유.
- 그 안에서 자동으로 Langfuse `generation` 기록 (모델·prompt·output·token).
- `cost.add(source=f"{provider}:{model}", usd=...)` 자동 누적.
- 역할별 모델이 달라도 langfuse span 의 `model` attribute 로 비용/지연이
  자동 분리 집계됨.

## 비-목표 (현 단계 제외)

- LLM provider 의 streaming 지원 (필요해질 때 도입)
- Function calling 의 multi-provider 정규화 (각 어댑터가 알아서 자기 포맷으로)
- Embedding LLM 통합 (embedding 은 `common/embedding/` 으로 분리)
- 자체 호스팅 LLM (vLLM/Ollama) — OpenAI 호환 endpoint 면 `OpenAILLM` 재사용 가능
