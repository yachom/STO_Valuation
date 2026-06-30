# 자산 계층 구조 — 공통 엔진 vs 자산 도메인

> 작성일: 2026-06-30 · 대상: `infra` / `real_estate` / `music_copyright`
> 배경: 모든 기초자산이 동일한 4단계 파이프라인을 공유하되, 자산 특수 부분은 각 자산
> 하위에 둔다는 설계 방향의 구조 정착(리팩토링 Phase A·B 반영).

## 1. 공통 4단계

모든 자산(부동산·음악저작권·…)은 같은 흐름을 탄다:

```
① 외부 데이터 수집  →  ② 그래프 DB 적재  →  ③ 그래프 DB 탐색  →  ④ 보고서 생성
```

수집(①)은 이층이다:
- **인터넷 탐색**(ddgs·Tavily·Naver·YouTube)은 전 자산 공통 → **infra 소유**.
- **자산별 정형 수집기**(인터넷에 안 나오는 자산 특수 소스)는 **각 자산 하위**에 두고
  공통 수집 단계에 끼워넣는다.

## 2. 경계 — 무엇이 infra, 무엇이 자산

### infra (자산 무관 엔진 + 공통 자원)

| 위치 | 역할 |
|---|---|
| `infra/graph/` | Neo4j 드라이버·스키마 적용·카드 레지스트리·walk(WalkPolicy)·벡터인덱스·임베더 (자산이 `GraphSchemaSpec`/`_BUILDERS`/`key_props`/`policy` 주입) |
| `infra/agent/base.py` | `BaseAgent` — LLM 호출·트레이싱·게이트웨이 throttle·재시도 |
| `infra/agent/roles/` | **공통 역할베이스** — `BaseWriterAgent`·`BaseSynthesizerAgent`(+`format_sections`). 양 자산이 동일하게 갖던 호출 골격을 보유 |
| `infra/agent/llm.py`·`tracing.py`·`gateway/` | LLM 라우팅·트레이싱·레이트리밋 |
| `infra/config_loader.py` | 슬롯별 레이어드 설정 메커니즘 |
| `infra/external/` | **인터넷 검색만** — ddgs·tavily·naver_search·naver_news_scraper·youtube + 공용 `_http` |
| `infra/tool/` | 인터넷 검색 도구(ddgs·web·naver·youtube) |
| `infra/asset.py` | `Asset` 베이스 |

### 자산 (`real_estate/`, `music_copyright/`)

| 위치 | 역할 |
|---|---|
| `<asset>/external/` | **자산 특수 정형 커넥터** (부동산: kakao_local·localdata·public_data_portal·reb_rone·seoul_open_data·building_*·molit·gov24 / 음악: spotify(resolver 내장)·musicbrainz·genius·soundcharts·wikidata·lastfm·deezer) |
| `<asset>/asset.py` + resolver/location | 자산 정체성 — 자유 텍스트 → 앵커 키(부동산: 주소→건물관리번호 / 음악: 제목+아티스트→ISWC/ISRC) |
| `<asset>/graphflow/graph/{schema,cards,queries}.py` | 자산 온톨로지·카드 빌더·읽기 Cypher |
| `<asset>/graphflow/{profiles,policies,axes}.py` | 축별 엣지 가중치·walk 정책·평가 축 |
| `<asset>/graphflow/{seeder,navigator,harvester,expand,composer,report,discourse,…}.py` | **자산별 오케스트레이션** — 의도적으로 자산마다 보유(엔진 추출 대상 아님) |
| `<asset>/graphflow/agents/base.py` | 자산 도메인 시스템 프롬프트 프레임(`music_system_prompt`/`korean_system_prompt`) |
| `config/<asset>_graphflow.json`·`<asset>_prompts.json` | 자산 설정·프롬프트 슬롯 |

## 3. 공통 에이전트 — infra 역할베이스 + 자산 상속

양 자산에 **동일 호출 골격**으로 존재하는 역할은 infra에 역할베이스를 두고 각 자산이
상속한다. 자산은 `name`·프롬프트 슬롯 키·자산별 출력 스키마·증거 포맷터·프레임 함수만
공급한다(graphflow 제어 흐름은 불변 — 에이전트 클래스의 base만 교체).

현재 베이스화된 역할(실코드 공유가 순이득인 것만):
- **`BaseWriterAgent`** — `write()` 골격(title/focus·system frame+role+rules·user
  card/evidence/outline/violations·complete).
- **`BaseSynthesizerAgent`** + **`format_sections`** — `synthesize()` 골격 + 두 자산이
  바이트 동일하게 갖던 섹션 직렬화.

> **스키마·프롬프트 문구는 자산별로 유지한다.** Pydantic `Field(description=…)`이
> structured-output으로 LLM에 전달되므로 통합하면 한 자산의 LLM 입력이 바뀐다. 베이스는
> 호출 *골격*만 공유하고 내용은 hook(`_get_prompt`/`_format_evidence`/`_frame`·클래스변수)
> 으로 받는다.

generic `BaseAgent` 그대로인 에이전트(베이스화하지 않음):
- **프롬프트 내용이 자산별로 크게 갈리는 공통 역할**(Outliner·Moderator·Questioner·
  Perspectivist) — 공유 실코드가 작아(후처리·에러정책뿐) 베이스가 간접층만 늘린다.
- **구현이 분기한 역할**(Navigator·Harvester — 출력 모델·파이프라인·수집 계약이 자산마다
  다름; Researcher — 증거 수집이 전량 도메인) 및 **자산 전용 에이전트**(음악
  axis_discoverer·agency_classifier·graph_proposer·reranker·social_extractor / 부동산
  prefilter·auditorllm·qa·reviser·walk_judge·doc_requester).

## 4. 안전망 — 특성화 테스트

`tests/characterization/`:
- `test_assembly_smoke.py` — 두 자산 `build_report_graph()` 컴파일 + 공통 9역할 인스턴스화·
  name·`BaseAgent` 상속 잠금.
- `test_connector_contracts.py` — 자산 정형 커넥터 6개의 공개 API가 `real_estate.external`
  에서 import됨(이전 후 계약).
- `test_agent_prompts.py` — **골든 프롬프트 캡처**: 공통 역할을 베이스로 끌어올릴 때
  `complete()` 직전 조립되는 (system, user, schema)가 동일함을 증명.

루트 `conftest.py`가 sys.path 앵커 + best-effort `.env` 로드.

## 5. 새 자산 추가 체크리스트

1. **정체성**: `<asset>/asset.py`(`Asset` 상속) + resolver(자유 텍스트 → 앵커 키).
2. **정형 수집기**: `<asset>/external/`에 자산 특수 커넥터를 둔다(infra/external 금지 —
   거긴 인터넷 검색 전용). 인터넷 검색은 `infra.tool.*`를 그대로 쓴다.
3. **온톨로지**: `<asset>/graphflow/graph/schema.py`에 `NODE_LABELS`/`EDGE_TYPES`/
   `KEY_PROP` → `GraphSchemaSpec`; `cards.py`에 `card_for_*` + `_BUILDERS`.
4. **평가**: `profiles.py`(축→엣지 가중치)·`policies.py`(`StaticPolicy`+`_HUB_LABELS`)·
   `axes.py`(평가 축).
5. **에이전트**: 공통 역할(Writer·Synthesizer)은 `infra.agent.roles`의 베이스를 상속해
   슬롯 키·스키마·hook만 채운다. 나머지·자산 전용은 generic `BaseAgent` 상속.
6. **오케스트레이션**: `<asset>/graphflow/{seeder,navigator,harvester,expand,composer,
   report,workflow,…}.py` — 기존 자산을 스켈레톤으로 복제·조정(엔진 자동 공유 아님 —
   자산별 튜닝 자유를 위해 의도적).
7. **설정**: `config/<asset>_graphflow.json`·`<asset>_prompts.json` 슬롯, `models.json`에
   `<asset>_*` 에이전트 티어.
8. **안전망**: `tests/characterization/`에 조립 스모크·(베이스 상속 역할이면) 골든 프롬프트.

## 6. 의도적으로 공유하지 않는 것

`*/graphflow/` 오케스트레이션(workflow·expand·navigator·harvester·composer·report·
discourse 등)은 자산마다 보유한다. 두 자산은 기능이 분기했고(음악: 동적 축·provenance
계약·agentic_graph/consolidate·별칭 grounding / 부동산: janitor·qa·reviser·validity·
HITL), 오케스트레이션을 공통 엔진으로 강제하면 자산별 튜닝 자유를 잃고 한 자산의 모델을
다른 자산에 강요하게 된다. 공유 이득은 **수집 계층(②의 인터넷 검색)과 에이전트 역할베이스**
에 집중한다.
