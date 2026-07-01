# 아키텍처 & 규약 — 온보딩 가이드

> 이 레포를 처음 보는 사람을 위한 지도. 전체 구조 → 규약(계약·제약) → 구현 방식 →
> 실행/읽기 순서. 세부 설계는 `docs/design/*`로 링크한다.
> (최종 갱신: 2026-07-01)

---

## 0. 이 레포는 무엇인가 (한 문단)

기초자산(**음악저작권**, **부동산**)을 **GraphRAG 파이프라인**으로 평가해 근거가 인용된
가치평가 보고서를 생성한다(STO 증권형 토큰 가치평가용). 모든 자산은 동일한 **4단계**를
공유한다:

```
① 외부 데이터 수집  →  ② 그래프DB 적재  →  ③ 그래프 탐색  →  ④ 보고서 생성
   (인터넷+정형API)     (자산 온톨로지)      (멀티홉 walk)     (LLM 집필·종합)
```

공통 엔진·규약은 `infra/`에, 자산별 도메인은 `music_copyright/`·`real_estate/`에 둔다.

---

## 1. 전체 코드 구조

### 1.1 최상위

| 디렉터리 | 역할 |
|---|---|
| `infra/` | **자산 무관 공통 엔진 + 규약** (게이트웨이·외부호출·알림·LLM·그래프·트레이싱) |
| `music_copyright/` | 음악저작권 자산 도메인 |
| `real_estate/` | 부동산 자산 도메인 |
| `config/` | 모델·프롬프트·파이프라인 튜닝 JSON (**코드 밖 설정**) |
| `scripts/` | 자산 무관 운영 스크립트 (예: `test_slack_webhook.py`) |
| `tests/` | 공통/특성화(characterization) 테스트 |
| `docs/` | 설계 문서 (`design/`) + 데이터소스(`data_sources/`) |

### 1.2 `infra/` — 공통 엔진

| 경로 | 역할 |
|---|---|
| `asset/base.py` | `Asset(asset_id, asset_type)` — 모든 자산의 얇은 베이스 |
| `external/_http.py` | 외부 HTTP transport(`timed_urlopen`/`timed_requests_get`) — 게이트웨이·타이밍·audit 경유 |
| `external/audit.py` | **외부호출 통합 기록·알림** (전수 로컬 jsonl + 오류 Slack) |
| `external/base_client.py` | `BaseExternalClient` — 외부 client 공통 베이스 |
| `external/{ddgs,tavily,naver_search,youtube}/` | 인터넷 검색 client (전 자산 공통, infra 소유) |
| `gateway/gateway.py` + `rate_limits.json` | `APIGateway` — provider별 호출 간격(rate limit) 강제 |
| `notify/slack.py` | `SlackNotifier`, `enable_slack_alerting` — Slack 웹훅 알림 |
| `agent/base.py` | `BaseAgent` — 모든 에이전트의 LLM 호출 베이스 |
| `agent/llm.py` | `get_llm(agent_name)` — 모델 티어 해소 + 콜백 부착 |
| `agent/tracing.py` | `RunTracer`/`emit`/`run_tracer` — 트레이스 + run_dir |
| `agent/roles/` | 역할별 베이스 에이전트(`BaseWriter`/`BaseSynthesizer`) — 양 자산 상속 |
| `graph/`, `config_loader.py`, `env.py`, `tool/`, `repository/` | 그래프 공통 스펙·설정 로더·.env 로더·도구·저장소 |

### 1.3 자산 패키지 (`music_copyright/`, `real_estate/`) — 의도적 미러 구조

두 자산은 같은 형태를 **미러링**한다(중복은 의도적, cross-import 0 — 한 자산을 고쳐도 다른
자산이 안 깨진다). 자세히: [`docs/design/asset_layering.md`](design/asset_layering.md).

| 경로 | 역할 |
|---|---|
| `asset.py` | `Asset` 서브클래스 (자산별 속성) |
| `external/<provider>/client.py` | **자산별 정형 커넥터** (인터넷에 안 나오는 식별자 기반 데이터). 음악: deezer·genius·lastfm·musicbrainz·soundcharts·wikidata / 부동산: kakao_local·molit_rtms·public_data_portal·reb_rone·localdata·seoul_open_data·lh·building_hub·… |
| `external/_http.py` (음악) | 자산 로컬 HTTP 헬퍼(`get_json`) — infra `_http`와 같은 역할 |
| `graphflow/` | **4단계 오케스트레이션** (아래) |
| `resolver.py` (음악) | 곡 식별(Spotify/MusicBrainz → work_id) |
| `scripts/` | 실행 진입점 (`try_music_report.py` 등) |
| `tests/` | 자산 단위/회귀 테스트 |

`graphflow/`의 주요 모듈(음악 기준; 부동산도 대응):
`navigator`(질의 계획) · `harvester`(웹수집+Fact추출+적재) · `expand`(웹심화) ·
`seeder`(정형 시드 적재) · `consolidate`(결정적 정리) · `agentic_graph`(LLM 엔티티 해소) ·
`traversal`/`composer`(그래프 탐색) · `rerank`(증거 재랭킹) · `writer`/`synthesizer`(집필·종합) ·
`report`(보고서 조립) · `axes`(평가축) · `coverage`(축별 충실도) · `social`(사회가치 추출) ·
`graph/`(`schema`·`driver`·`cards`·`queries`).

---

## 2. 규약 (계약 & 제약) — 역할별 코드가 지켜야 할 것

### 2.1 자산 식별 규약
모든 자산은 `Asset(asset_id, asset_type)`(infra/asset/base.py)를 상속한다. `asset_id`는 안정 키
(예: `mc:ISRC:...`, `re:...`), 로거는 `asset.{asset_type}.{asset_id}`. 외부호출·트레이스·보고서가
이 식별자로 "어느 자산의 작업인지"를 단다.

### 2.2 ★ 외부 데이터 호출 규약 (핵심)

**모든 외부 API 호출**(인터넷 검색 + 자산 정형 커넥터)은 다음 6가지를 지킨다:

| # | 제약 | 왜 |
|---|---|---|
| 1 | **게이트웨이 통과** — `gateway.wait(api_name)` | provider별 최소 호출 간격 강제(전역 throttle) → 429 방지 |
| 2 | **전수 로컬 기록** — 모든 응답(성공·실패)을 `run_dir/responses.jsonl`에 | 추후 에러코드 분석용 영구 보관 |
| 3 | **오류만 Slack** — 비-2xx/예외만 `api_limit`+`alerts` 두 채널 동시 | 전수 Slack은 ~1msg/초 한도로 429 드랍·지연 → 오류만 |
| 4 | **자기설명 메시지** — asset·phase·node·axis·run_id·api.operation·endpoint·status·응답발췌·elapsed_ms | 메시지만 보고 "어디서 무엇이" 문제인지 파악 |
| 5 | **시크릿 redact** — 키/토큰/serviceKey/Bearer 마스킹 | 로그·Slack 유출 방지 |
| 6 | **best-effort** — 기록/알림은 절대 호출부를 깨지 않음(예외 삼킴) | 관측이 본 기능을 망가뜨리지 않게 |

**오류 정의(잠정):** HTTP 2xx 이외 + 예외/네트워크 실패. (provider별 에러코드 세분화는 추후 과제.)
**채널 의미:** `api_limit`=모든 외부 오류를 모으는 채널(이름과 달리 전 오류용), `alerts`=가시성
채널(오류 + 실행종료 요약 1건).

**client가 이 규약을 충족하는 길** — 셋 중 하나(전부 audit로 수렴):
- `BaseExternalClient` 상속 → `_get_requests`/`_urlopen`(HTTP) 또는 `_audited`(라이브러리) 사용
- infra `_http` transport 직접 사용(naver/tavily)
- 음악 `_http.get_json` 사용(genius/lastfm/…)

현재 **외부 client 17종 전부 audit 커버** (infra/_http 7 + music/_http 6 + BaseExternalClient 4).

### 2.3 LLM 호출 규약
- 모든 에이전트는 `BaseAgent`(infra/agent/base.py) 상속, `get_llm(agent_name)`(infra/agent/llm.py)
  으로만 LLM을 부른다.
- **모델 ID를 코드에 하드코딩하지 않는다.** `config/models.json`의 티어 매핑(`agents.<name> → tier`)
  으로 결정, env override 가능(`AGENT_TIER_<NAME>`/`TIER_<TIER>_*`).
- LLM 호출은 tracing 콜백을 거쳐 자동 트레이스되고, **LLM 에러는 emit 훅으로 Slack** 통지.

### 2.4 그래프 적재 규약 — 엔티티 해소 2계층
- **A. 적재-시점 mention 링킹** = 결정적 코드(`harvester._match_scope_name`). 별칭/괄호/한글-영어
  표기 매칭은 여기(LLM 아님).
- **B. 사후 정리** = LLM proposer(`agentic_graph`) + 코드 validator. validator는 점수가 아니라
  **불변식**만 본다: 허용 엣지인가 · 스키마상 합법 (src→dst)인가(`graph/schema.py EDGE_ENDPOINTS`)
  · 근거 Fact+source_excerpt 존재 · 작품에 구조적 앵커. 객관 엣지만 자동커밋, 해석성은 review.
- **자율성 원칙:** 하드코딩 토큰/사전 금지, 불확실하면 review로 사람 검토.
- 상세: [`entity_resolution_layers.md`](design/entity_resolution_layers.md),
  [`agentic_graph_autonomy_decision.md`](design/agentic_graph_autonomy_decision.md).

### 2.5 설정/프롬프트 규약
`config/*.json`이 코드 밖 설정의 단일 위치다. 우선순위 **env > json > 코드 `_DEFAULTS`**.

| 파일 | 내용 |
|---|---|
| `models.json` | LLM 티어 정의 + 에이전트→티어 매핑 |
| `prompts.json` / `music_prompts.json` | 에이전트 시스템 프롬프트 |
| `graphflow.json` / `music_graphflow.json` | 파이프라인 튜닝(축·검색·확장·QA 한도 등) |
| `gateway/rate_limits.json` | provider별 호출 간격 |

---

## 3. 규약이 구현되는 방식 (어디서 강제되나)

| 규약 | 구현 위치 (파일 · 심볼) |
|---|---|
| 게이트웨이(rate limit) | `infra/gateway/gateway.py` `APIGateway.wait` + `rate_limits.json` |
| 외부호출 전수 기록·오류 알림 | `infra/external/audit.py` `record_response`/`set_external_context`/`flush_summary` |
| 외부 transport | `infra/external/_http.py` `timed_urlopen`/`timed_requests_get`; `music_copyright/external/_http.py` `get_json` |
| 외부 client 베이스 | `infra/external/base_client.py` `BaseExternalClient`(`_get_requests`/`_urlopen`/`_audited`) |
| Slack 알림 | `infra/notify/slack.py` `SlackNotifier`(`classify_channel`/`redact`/dedup/`notify_error(force_channels=)`), `enable_slack_alerting` |
| 파이프라인 배선 | 진입부에서 `enable_slack_alerting()` + `audit.set_external_context()` + 종료 `audit.flush_summary()` — 음악 `music_copyright/scripts/try_music_report.py`, 부동산 `real_estate/graphflow/workflow.py` |
| LLM 호출·티어 | `infra/agent/llm.py` `get_llm`; `infra/agent/tracing.py` `emit`/`RunTracer`; `config/models.json` |
| 그래프 적재 검증 | `harvester._match_scope_name`(A) / `agentic_graph._validate_promotion`·`EDGE_ENDPOINTS`(B) |

**관측 산출물(실행마다):** `music_copyright/reports/<run_id>/`에 `report.md`/`report.json`(보고서),
`responses.jsonl`(외부호출 전수), `trace.jsonl`(파이프라인 트레이스).

---

## 4. 신규 진입자 가이드

### 4.1 셋업
- **Python**: conda base `/opt/miniconda3/bin/python` 사용(시스템 python3 아님). `requirements.txt` 없음 — base 환경에 의존성 설치돼 있음.
- **Neo4j(docker)**: 부동산 `docker compose up -d neo4j`(7687), 음악 `docker compose up -d neo4j-music`(7688).
- **`.env`**: `.env.example` 복사 후 키 채움 — Spotify/MusicBrainz/OpenAI/Anthropic, 선택으로 `SLACK_WEBHOOK_API_LIMIT`/`SLACK_WEBHOOK_ALERTS`, `LANGFUSE_*`. (`.env`는 gitignore.)

### 4.2 실행 (음악 end-to-end)
```bash
/opt/miniconda3/bin/python -m music_copyright.scripts.try_music_report \
    --title "LOVE ATTACK" --artist "RESCENE" --expand-rounds 3
```
산출물은 `music_copyright/reports/<run_id>/`. (`--workflow` LangGraph 경로, `--discourse` STORM 담론,
`--axis "라벨::의도"` 평가축 추가, `--discover-axes` 에이전트 축 자동제안.)

### 4.3 테스트
```bash
/opt/miniconda3/bin/python -m pytest music_copyright/tests/ tests/ -q   # 단위 + 특성화
/opt/miniconda3/bin/python -m scripts.test_slack_webhook --target clients  # Slack 웹훅 3계층 커버리지
```
`test_slack_webhook.py`는 무해한 API를 잘못된 키로 호출해(LLM 무개입) 외부호출→audit→Slack
경로를 검증한다(`--dry-run`으로 무전송 라우팅만).

### 4.4 어디서부터 읽나 (추천 경로)
1. **이 문서** (전체 지도)
2. [`design/asset_layering.md`](design/asset_layering.md) — 공통 엔진 vs 자산 도메인 경계
3. [`design/graph_native_flow.md`](design/graph_native_flow.md) — 그래프 네이티브 파이프라인 흐름
4. [`design/entity_resolution_layers.md`](design/entity_resolution_layers.md) — 적재(엔티티 해소) 2계층
5. [`design/music_valuation_pipeline_review.md`](design/music_valuation_pipeline_review.md) — 음악 평가 재설계 리뷰
6. 코드: `try_music_report.py`(진입) → `graphflow/workflow.py`(흐름) → `harvester.py`/`agentic_graph.py`(적재) → `report.py`(보고서)

### 4.5 용어 사전
- **work_card**: 평가 대상의 카드 텍스트(제목·아티스트·별칭 등) — 검색·grounding의 기준.
- **scope**: 평가 대상 노드 주변의 그래프 이웃(scope_eids/scope_hops).
- **mention**: 문서에서 추출된 표면형(곡명·아티스트·차트…) — 노드에 링크되거나 고아 Entity가 됨.
- **Fact / Entity**: Fact=원자적 사실(+source_excerpt), Entity=미해소 추출물(해소되면 typed 노드로 승격).
- **provenance**: `Fact-EXTRACTED_FROM->WebDoc`, `Fact-EXTRACTED_BY->Query` 등 출처 추적 체인.
- **axis(평가축)**: 음악은 **자산가치 트랙**(영향력·대중성/팬덤·지속성) + **사회·ESG 트랙**
  (사회적가치·혁신·ESG/리스크)의 다중 축. 사람(`--axis`)·에이전트(`--discover-axes`)가 동적 추가.
  프레임은 `esg_assessment.md`(S 주축·G 필수)·`social_value.md`(social-primary). (부동산은 tenant/esg/commercial.)
- **proposer**: `agentic_graph`의 LLM — (엔티티→관계→노드)를 제안, validator가 검증.
- **audit**: 외부호출 전수 기록 + 오류 Slack 통지의 단일 경유점(`infra/external/audit.py`).

### 4.6 설계 문서 색인 (`docs/design/`)
**평가 프레임(사회적 가치·ESG)**
- `esg_assessment.md` — ESG(S 주축·G 필수·E material) 공통 프레임 ★상위
- `social_value.md` — 사회적 가치 평가 통합 설계(S-pillar, 음악 우선)
- `structured_ingestion.md` — 외부 정형데이터 수집·적재(사회가치 관점)

**자산 공통 구조**
- `asset_layering.md` — 공통 엔진 vs 자산 도메인 경계
- `asset_extension.md` — 새 기초자산 추가 체크리스트(정형=seed·웹=expand 타이밍)

**파이프라인·탐색**
- `graph_native_flow.md` — 그래프 네이티브 파이프라인 흐름(결정 로그 D1~D22)
- `agentic_traversal.md` — 적응 깊이 + PPR(compose-recall 플래그 뒤 · **frontier PPR 항상 가동**)
- `music_valuation_pipeline_review.md` — 음악 평가 파이프라인 리뷰
- `korean_music_sources.md` — 한국 음악 웹서치 소스(뉴스·커뮤니티·정형)

**엔티티 해소**
- `entity_resolution_layers.md` — 2계층 as-built ★현행
- `entity_resolution_gap.md` — 갭 진단 · `entity_resolution_plan.md` — 구현 전 계획(완료)
- `agentic_graph_autonomy_decision.md` — 자율성(하드코딩 금지·불확실→review) **결정**(유효). 단
  구현 세부(§8~11 라벨/토큰 기준)는 레거시 → as-built은 위 `entity_resolution_layers.md`(엣지/불변식).

**🗄️ 레거시(역사적 기록)**
- `HANDOFF_agentic_curation.md` — STORM 인계(구현 완료) · `agentic_curation.md` — STORM 설계(구현됨)
