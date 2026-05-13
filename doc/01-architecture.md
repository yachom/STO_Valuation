# 01. Architecture

## 최상단 3분기

```
STO_valuation/
├── common/      # 공통 인프라 + 자산-불문 최소 추상 (베이스 클래스, ports, 횡단 관심)
├── projects/    # 자산별 구체화. domain 은 전적으로 자산이 소유
├── airflow/     # 배치 DAG + 진입 스크립트 (실 로직은 common/projects 위임)
├── doc/         # 본 문서 set
└── tests/
```

## 폴더 트리 (전체)

```
common/
├── domain/                          # 자산-불문 최소 추상
│   ├── asset.py                     # AssetClass, AssetProfile
│   ├── target.py                    # Target ABC
│   └── graph.py                     # GraphNode, GraphEdge, RelationType
├── ports/                           # 외부 의존 인터페이스 (Protocol/ABC) — 구현 0줄
│   ├── llm.py
│   ├── embedding.py
│   ├── repository.py                # NewsRepo / SignalStore
│   ├── graph.py                     # GraphWriter / GraphReader
│   ├── tool.py
│   ├── mcp.py
│   └── news.py                      # OntologyTagger / GraphSignalWriter (자산 구현)
├── clients/                         # 외부 호출 어댑터 (평탄)
│   ├── base.py                      # BaseClient (httpx + retry + quota + log)
│   ├── naver_news.py
│   └── web_search.py
├── llm/                             # LLM 어댑터 (평탄)
│   ├── base.py                      # BaseLLM (cache/retry/langfuse/cost)
│   ├── anthropic.py                 # AnthropicLLM
│   ├── openai.py                    # OpenAILLM
│   ├── router.py                    # RouterLLM (stub)
│   ├── registry.py                  # LLMRegistry (YAML → 인스턴스)
│   └── prompts/
├── mcp/
│   ├── server_base.py               # BaseMCPServer
│   └── client.py                    # 외부 MCP 서버 연결 어댑터
├── repositories/                    # ports.repository 구현체
│   ├── base.py
│   └── memory.py                    # MemoryNewsRepository / MemorySignalStore
├── graph/                           # ports.graph 구현체
│   ├── base.py
│   └── memory.py                    # MemoryGraphStore
├── embedding/                       # ports.embedding 구현체
│   ├── base.py
│   └── fake.py
├── services/                        # 자산-불문 서비스
│   ├── news/                        # 뉴스 파이프라인 (자산 tagger/graph_signal 주입)
│   │   ├── normalizer.py            # pure func: normalize_html, normalize_item
│   │   ├── filter_rules.py          # pure func: apply(news, rules) -> FilterResult
│   │   ├── dedup.py                 # pure func: cosine, is_duplicate
│   │   └── pipeline.py              # run(profile, date, tagger, graph_signal, ...)
│   └── embedding.py                 # EmbeddingService
├── tools/
│   └── base.py                      # BaseTool
├── agents/
│   ├── base_agent.py
│   ├── planner_base.py              # PlannerBase + PlannedAction
│   ├── retriever_base.py            # RetrieverBase + ToolResult
│   ├── critic_base.py               # CriticBase + Verdict
│   └── policy.py                    # PolicyGate, Budget, Decision
├── flows/                           # LangGraph 베이스
│   ├── exploration.py               # build_exploration_graph(planner, retriever, critic, gate)
│   └── qa.py                        # build_qa_graph(...)
├── observability/                   # cross-cutting
│   ├── logging.py                   # structlog
│   ├── langfuse.py                  # 선택적 (env 없으면 no-op)
│   ├── tracing.py                   # @traced + span CM
│   ├── context.py                   # correlation_id / scope_id ContextVar
│   ├── cost.py                      # 요청 scope 단위 LLM/API 비용 누적
│   └── quota.py                     # 외부 API 일별 쿼터 카운터
├── config/
│   └── settings.py                  # pydantic-settings (env, API keys)
├── errors.py                        # STOError 계층
└── utils/                           # 해시 / url / 시간 (pure func)

projects/
├── commercial/                      # MVP 실 구현
│   ├── profile.py                   # COMMERCIAL_PROFILE
│   ├── domain/
│   │   ├── targets.py               # BuildingTarget(PNU), TenantTarget(bizno)
│   │   ├── factors.py               # COMMERCIAL_EVALUATION_FACTORS
│   │   ├── ontology.py              # 6축 enum (Society/Policy/Tenant/Market/Macro/Local)
│   │   ├── signals.py               # MarketSignal, Evidence (부동산 형)
│   │   ├── claims.py                # EvaluationClaim, AnalysisResult
│   │   └── relations.py             # LOCATED_IN, LEASED_BY 등 자산 특화 라벨
│   ├── clients/                     # BaseClient 상속
│   │   ├── juso.py
│   │   ├── vworld.py
│   │   ├── molit.py
│   │   ├── building_ledger.py
│   │   └── naver_news.py            # search_groups 와 결합한 래퍼
│   ├── services/
│   │   ├── initial_collector.py     # JUSO→VWorld→MOLIT→대장 시퀀스
│   │   ├── ontology_tagging.py      # ports.news.OntologyTagger 구현
│   │   ├── graph_signal.py          # ports.news.GraphSignalWriter 구현
│   │   └── report_builder.py
│   ├── tools/                       # BaseTool 상속 (선별적)
│   │   ├── collect_address_metadata_tool.py
│   │   └── search_commercial_news_tool.py
│   ├── agents/                      # PlannerBase/RetrieverBase/CriticBase 상속
│   │   ├── planner.py
│   │   ├── retriever.py
│   │   └── critic.py
│   ├── flows/
│   │   └── analyze_address.py
│   ├── mcp/
│   │   └── server.py
│   ├── config/
│   │   ├── search_groups.yaml
│   │   └── llm.yaml                 # 역할별 LLM 매핑
│   └── utils/
└── music/                           # placeholder (같은 골조의 빈 파일)
    └── …

airflow/
├── dags/
│   ├── commercial_news_daily.py
│   └── music_news_daily.py
└── scripts/
    └── run_news_pipeline.py

composition.py                       # build_<asset>(settings) + PROFILE_REGISTRY
cli.py                               # analyze-address / collect-news
tests/{common, projects/commercial, smoke}
pyproject.toml, .env.example, README.md
```

## 의존성 그래프 (불변)

화살표 = "import 한다". **항상 위에서 아래로만**.

```
Layer 8   airflow/dags
Layer 7   airflow/scripts, composition.py, cli.py
Layer 6   projects/<x>/{flows, agents, tools, services, clients, mcp}
Layer 5   projects/<x>/domain
Layer 4   common/{flows, agents}
Layer 3   common/{services, tools}
Layer 2   common/{clients, llm, mcp, repositories, graph, embedding}
Layer 1   common/ports
Layer 0   common/{domain, errors, utils}

Cross-cutting (어디서나 import 가능, 자기는 거의 import 안 함):
          common/{observability, config}
```

### 핵심 불변

1. **한 방향만**: 위 레이어가 아래 레이어만 import. 역방향 0.
2. **`common` 은 `projects` 를 모름**. `common` 안에 `projects` import 0건.
3. **`projects/A` 는 `projects/B` 를 모름**. 자산 간 cross import 0건.
4. **`ports` 는 인터페이스만**. Protocol/ABC 외 구현 코드 0줄.
5. **`observability`, `config`** 는 cross-cutting. 자기는 stdlib·외부 SDK 외엔 import 거의 X.

자동 검증 (도입 예정): `import-linter` 또는 `ruff TID`.

## 폴더별 책임/금지 표

| 폴더 | 허용 import | 금지 import |
|---|---|---|
| `common/domain` | pydantic, stdlib | 다른 common 하위, projects |
| `common/ports` | typing/Protocol, `common/{domain,errors}` | 구현체 SDK |
| `common/clients` | httpx, `common/{ports,observability,errors,config}` | services/agents/tools |
| `common/llm` | provider SDK, `common/{ports,observability,config}` | services/agents/tools |
| `common/services` | `common/{clients,ports,domain,observability,repositories,graph,embedding}` | tools/agents/flows, projects |
| `common/tools` | `common/{services,domain,observability,ports}` | clients/llm 직접 |
| `common/agents` | `common/{tools,llm,observability,ports}` | clients 직접 |
| `common/flows` | `common/{agents,observability}` | clients/services 직접 |
| `common/observability,config` | stdlib, 외부 SDK | 다른 common 하위 거의 X |
| `projects/<x>/*` | common 전부 + 자기 자산 하위 | 다른 자산 |
| `airflow/scripts` | composition + projects 진입 함수만 | 내부 구현 직접 import |

## 4계층 외부 연결 책임

| 계층 | 책임 | 입력 | 출력 | domain 의존 |
|---|---|---|---|---|
| **clients** | 외부 1회 호출. 인증·재시도·rate-limit·로깅 | typed args | raw dict / client-local pydantic | X |
| **services** | clients 합성, 정규화, 필터, 조립 — 비즈니스 로직 | typed args + 주입 의존성 | **domain model** | O |
| **tools** | services 의 LLM 래퍼. 입력 검증·인자 변환만 | LLM 친화 인자 | domain model 의 JSON | O |
| **mcp** | tools 의 외부 프로세스 노출 | MCP 프로토콜 | MCP 프로토콜 | 간접 |

엄격 규칙:
- clients 는 **절대 domain 을 import 하지 않음** (Anti-Corruption Layer).
- services 만 raw→domain 매핑을 책임. 매핑은 service 내부 pure 함수.
- tools 안에 비즈니스 로직 0 lines.
- mcp 안에 비즈니스 로직 0 lines.

raw 응답을 위한 별도 폴더는 두지 않는다. service 가 즉시 domain 으로 변환.
