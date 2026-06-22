# 인계 문서 — 담론 주도 큐레이션(STORM/Co-STORM) 구현

> 작성: 2026-06-21. 다음 세션이 **이 문서만 읽고** STORM 큐레이션 구현을 이어갈 수 있도록 자급식으로 작성.
> 한 줄 요약: 설계 합의 완료(`agentic_curation.md`, D17~D22). **다음 작업 = PR-2(Moderator + GAP 재진입)부터 구현.** 작업 브랜치 `yachom/feature/agentic-report`.

---

## 0. 지금 당장 할 일 (다음 세션 시작점)

```bash
git fetch origin
git checkout yachom/feature/agentic-report   # 모든 것이 여기 모임: 설계 + writer ① + langfuse 헬퍼 + main
git pull
# 이 위에서 PR-2 구현 브랜치를 딴다:
git checkout -b yachom/feature/storm-moderator
```
`agentic-report`는 main을 병합해 **refactor·langfuse·writer ①·설계·인계를 전부 포함** — 이 브랜치 하나만 보면 됨.
설계 정독: `docs/design/agentic_curation.md` + `docs/design/graph_native_flow.md`의 결정 로그 **D17~D22**.

---

## 1. Git / 버전화 상태 (점검 완료)

| 브랜치 | 내용 | origin | 상태 |
|---|---|---|---|
| `main` | graphflow 전체 + refactor + **langfuse trace.name 수정(병합됨)** | ✅ 푸시됨 (036192a) | 동기화 |
| `yachom/fix/langfuse-trace-name` | trace.name 회귀 수정 (2커밋) | ✅ | **main 병합 완료** — 보존(삭제 가능) |
| `yachom/feature/agentic-report` | writer ① + STORM 설계 + 인계 + **main 병합(langfuse 포함)** | ✅ 푸시함 | **작업 브랜치 — 전부 포함** |

- **main = origin 일치 (036192a).** refactor + langfuse 수정 포함. graphflow 전체가 main에 있음.
- **`agentic-report`는 main을 병합**해 langfuse 헬퍼·writer ①·설계·인계를 모두 포함 → 다음 세션은 이 브랜치 하나로 STORM 시작.
- **커밋 제외 영구 규칙**: `.env.example`, `STO_valuation_orgin`, `CLAUDE.md`(untracked). 작업트리에 항상 M/?? 로 남지만 **절대 커밋하지 말 것.**

### 병합 상태
- ✅ `langfuse-trace-name` → **main 병합·푸시 완료** (036192a). langfuse 헬퍼를 main·agentic-report에서 바로 사용 가능.
- `agentic-report`는 main 병합 완료. STORM 구현은 이 브랜치에서 PR 브랜치를 따서 진행, 완료분은 PR 단위로 main 병합.

## 2. 코드 작동 상태 (점검 결과)

**정적 검증 — 통과 (이번 세션 실행):**
- `python -m py_compile` : graphflow 전체 + scripts ✅
- 전 모듈 import + `build_report_graph()` 컴파일 ✅
- writer ① 캡 반영 확인 ✅

**런타임 검증 — 미수행 (인프라 필요).** 보고서 end-to-end는 Neo4j + API키(KAKAO/JUSO/DATA_GO_KR/NAVER…) + Anthropic/OpenAI 키 + (선택)Langfuse 키 필요. 다음 세션에서 스모크:
```bash
docker-compose up -d neo4j
# .env 채운 뒤
python -m scripts.try_report "서울 강서구 마곡동 774-1"   # trace_name=report:주소 로 run_tracer 래핑됨
# 또는 streamlit run scripts/app.py
```
- 리팩터 동등성은 이전에 차등 테스트로 검증됨(순수 함수 96케이스 byte 동일 + rename 본문 동일). 그 일회성 스크립트는 트리에 안 남김(검증 완료).

## 3. 설계 요약 (구현 가이드)

상세는 `agentic_curation.md`. 핵심 결정(결정 로그):

- **D17 담론 큐레이션**: 고정 EXPAND(navigator→harvester 1패스)를 **관점 발견 → 시뮬레이션 Q&A → 모더레이터 → 아웃라이너** 담론 루프로. Neo4j 그래프 = Co-STORM 동적 마인드맵 재사용.
- **D18 3축 척추 + 추가 발견**: tenant/esg/commercial 3축은 **구조화 데이터에 1:1 매핑된 고정 척추**로 유지. 발견 관점은 **additive 보충**(상한 N), 대체 아님.
- **D19 모더레이터 = 커버리지 기반 unknown-unknowns**: auditor(정합성)와 역할 분리. 그래프 커버리지 메트릭으로 빈 곳 질문 주입·종료 판정.
- **D20 재현성**: 담론은 `config.json`의 `curation.mode: static|discourse` 플래그 뒤(기본 static). 프로덕션은 기존 결정 경로. PR마다 flag 격리.
- **D21 writer 깊이**: max_tokens 아닌 구조 캡이 병목 → 완화(이미 적용, 56d826e). grounding 불변.
- **D22 도구 한계**: 자율 데이터 획득은 **검색뿐**(구조화 소스는 고정 배선·bind_tools 미사용). 발견 관점은 검색으로만 채움. 완전 tool-agent는 ROI 낮아 보류.

## 4. 구현 계획 (PR 단위 — flag로 격리, 기존 경로 항상 작동)

| 단계 | 범위 | 산출 |
|---|---|---|
| **PR-1** | 커버리지 메트릭 모듈(Cypher) + Langfuse score 적재 | moderator 측정 토대. 기존 흐름 불변 |
| **PR-2 (시작점)** | **Moderator** 에이전트 + GAP→미니EXPAND 재진입 (flag 뒤, 기존 EXPAND에 얹음) | 가장 큰 에이전틱 이득 + 독립 가치(미뤄둔 GAP 루프 완성) |
| PR-3 | Perspectivist + Questioner/Researcher discourse loop | EXPAND 교체(flag) |
| PR-4 | Outliner + writer 아웃라인 입력 | 구조 동적화 |
| PR-5 | A/B 벤치마크(static vs discourse) — 비용·깊이·커버리지 | variant 검증 |

### PR-2 구체 착수 가이드
1. **커버리지 메트릭** (PR-1 일부를 먼저): 새 모듈 예 `real_estate/graphflow/coverage.py` — Cypher로 관점별 Fact 밀도·미인용 노드·신선도 공백 산출. (자산 스코프 쿼리는 `traversal.scope_eids`/기존 패턴 참고.)
2. **Moderator 에이전트**: `real_estate/graphflow/moderator.py`, `class Moderator(BaseAgent)`, `name="moderator"`. `self.complete(system=get_prompt("moderator.system"), user=..., schema=...)`. 입력=커버리지 신호+스코프 요약, 출력=다음 질문 리스트 + 종료여부(structured).
3. **GAP 재진입**: 기존 `expand.run_expand(...)`를 moderator가 만든 질문/purpose로 호출(QA의 자가확장 `qa.py`가 `run_expand(purpose=..., rounds=1, ...)` 호출하는 패턴 그대로 재사용).
4. **flag 배선**: `config.json`에 `"curation": {"mode": "static"}` 추가, `config.py _DEFAULTS`에도. `workflow.expand_node`에서 `mode=="discourse"`면 moderator 루프 경유.
5. **등록**: `config.json` `models.agents`에 `"moderator": "haiku"` (저단가 다턴). `prompts.json`+`prompts.py _DEFAULTS`에 `moderator.system` 추가(둘 다 — json이 런타임 권위).

## 5. 코드베이스 핵심 사실 (구현에 필요)

- **에이전트 작성**: `agents/base.py`의 `BaseAgent` 상속, 클래스변수 `name` 설정, `self.complete(system=, user=, schema=Pydantic)` 한 줄. LLM/langfuse 직접 import 금지 — tier는 config가 결정.
- **LLM 진입점**: `get_llm("agent_name")` (llm.py). generation name=agent_name 이어야 **Langfuse 비용 집계**됨.
- **모델 매핑**: `config.json` `models.agents` (navigator=haiku, harvester/prefilter=gpt_mini, curator/walk_judge/qa_recognize=haiku, writer/auditor/reviser/qa=sonnet, synthesizer=opus). 우선순위 env > config.json > 코드 `_DEFAULTS`.
- **프롬프트**: `get_prompt("key")`만 호출. 문구는 `prompts.json`(런타임 권위) + `prompts.py _DEFAULTS`(폴백) **둘 다** 수정해 동기 유지.
- **워크플로우 조립**: `workflow.py build_report_graph()` — `START→seed→tenant→expand→[write_tenant∥write_esg∥write_commercial]→synthesize→finalize`. EXPAND가 `expand.run_expand`(navigator→harvester→curator 라운드 루프) 호출.
- **그래프/walk**: `traversal.py`(walk 엔진), `policies.py`(StaticPolicy/SemanticPolicy, WalkPolicy Protocol). 보고서=결정적 정책. `llm_walk`는 QA/revise 전용(비결정적).
- **수집**: `expand.run_expand`, `navigator.py`(frontier+검색어), `harvester.py`(추출+link_mention 오염방지), `curator.py`(novelty 중복제거).
- **아키텍처 현실**: `bind_tools`/LLM 함수호출 **전무**. 도구(BaseTool: building_*, naver, web)는 전부 Python 명령형 호출. 자율 데이터 획득=검색뿐(D22).
- **트레이싱**: `infra/agent/tracing.py`. `run_tracer(run_id, db_path, trace_name=, session_id=, tags=)`로 감싸면 Langfuse trace.name + 사이드카. ⚠️ **새 LangGraph `app.invoke`를 추가하면** `langchain_trace_metadata()`(infra/agent/tracing.py)를 invoke config의 `metadata`로 넘겨야 trace.name이 붙음. 이 헬퍼는 **이제 main·agentic-report에 있음**(langfuse 병합 완료) → 바로 사용. 참고 패턴: `workflow.run_report_pipeline`(metadata 주입 + asset_id 폴백).

## 6. 컨벤션 / 함정

- **Import**: bare top-level (`from real_estate...`, `from infra...`). 프리픽스 금지.
- **커밋 메시지 끝**: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **브랜치 네이밍**: `<user>/<type>/<scope>` (예: `yachom/feature/storm-moderator`). 이름과 범위 일치.
- **커밋 금지 파일**: `.env.example`, `STO_valuation_orgin`, `CLAUDE.md`.
- **prompts.json이 prompts.py를 덮음** — 문구 바꿀 땐 둘 다.
- **base 추출 보류**: 2+ 사례 동시 관찰 전엔 공통 base 안 뺌(1사례 과적합 회피).
- **프롬프트 변경 = 동작 변경** — 수정 시 신중히(단 STORM은 의도적 신규라 동등성 대상 아님).

## 7. 환경 (런타임)
- `.env`에 키: `KAKAO_API_KEY`, `JUSO_API_KEY`, `DATA_GO_KR_API_KEY`, `NAVER_*`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`(gpt_mini), `NEO4J_*`, (선택)`LANGFUSE_*`, (선택)`TAVILY_API_KEY`. `infra.env.load_env`로 로드.
- Neo4j: `docker-compose up -d neo4j`.
- Python 실행: `python -m scripts.try_xxx ...` (모듈 실행).

## 8. 미해결 / 별도 트랙 (구현과 독립)
- **측정 기준 보류**: 깊이·커버리지의 "좋음"을 무엇으로 잴지 미결정. PR-5 A/B는 일단 비용·깊이·커버리지 분리축만.
- **Langfuse 비용 누수**: 최근 7일에도 generation의 ~13%가 에이전트명 아닌 `ChatAnthropic`으로 집계됨($1.15/주). 부모 span은 정상이라 trace/세션 비용은 정확. 원인(structured-output/특정 경로) 코드 점검은 별도 소소한 과제.
- **현재 비용 1위는 writer($5.20/주, sonnet) > synthesizer($3.00, opus)** — 메모리의 "harvester 1위"는 옛 상태. writer/synthesizer 모델 tier가 A/B 후보.
- Langfuse MCP로 비용·trace 즉석 조회 가능(getHealth/queryMetrics/listObservations). datasets·score config는 현재 0개(그린필드) — A/B 인프라 부트스트랩 대상.

## 9. 이번 세션에서 한 일 (맥락)
1. graphflow 유지보수 리팩터(死코드 ppr.py 삭제, 공통 헬퍼, reviser→feedback 분리, private 심볼 public 승격) → **main 병합·푸시 완료**.
2. Langfuse trace.name 회귀 수정 + Streamlit 4동작 래핑 → `langfuse-trace-name` 브랜치(미병합).
3. writer 깊이 캡 완화(①) → `agentic-report` 브랜치.
4. STORM/Co-STORM 기반 구조 고도화 **설계** + 결정 로그 D17~D22 → `agentic-report` 브랜치.
5. 이 인계 문서 작성.
