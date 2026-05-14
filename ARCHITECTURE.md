# STO 사회적 가치 평가 시스템 — 아키텍처 문서

본 문서는 사용자가 **주소 한 줄을 입력**한 시점부터 **최종 보고서·QA 응답이 반환되기까지**의 모든 경로를, 호출되는 파일·함수·매개변수·출력 형태와 함께 단계별로 정리한다.

---

## 0. 개요 — 입력에서 출력까지 한눈에

```
[Streamlit UI: app.py]
  사용자 입력: "서울특별시 강남구 테헤란로 152"
        │
        ▼
[src/graph.py :: run(address)]   ──── LangGraph 파이프라인 시작
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ Node 1: metadata     src/metadata/resolver.py             │
│   주소 → 주소API → PNU → 토지/건축물대장/카카오 좌표        │
├───────────────────────────────────────────────────────────┤
│ Node 2: news         src/news/pipeline.py                 │
│   네이버 뉴스 크롤 → LLM 온톨로지 태깅 → NetworkX KG 적재   │
├───────────────────────────────────────────────────────────┤
│ Node 3-5: 3축 평가  src/agents/axis_subgraph.py            │
│   각 축마다 Planner → ReAct Retriever → Scorer (병렬)       │
│   - ESG / 인지도 / 상권·입지                                │
├───────────────────────────────────────────────────────────┤
│ Node 6: report       src/agents/report.py                 │
│   3축 결과 + KG 인사이트 → Claude로 마크다운 보고서 합성    │
└───────────────────────────────────────────────────────────┘
        │
        ▼
[src/web/run_store.py :: save_run]
   data/runs/<주소해시>/ 에 report.md / result.json / knowledge_graph.json 저장
        │
        ▼
[Streamlit UI: 4개 탭]   ──── 보고서·평가상세·지식그래프·QA
        │
        │ QA 탭에서 사용자가 자유 질의
        ▼
[src/qa/graph.py :: answer(question, session)]
   Router → 전문가 N명 fan-out → Synthesizer
```

---

## 1. 진입점 — Streamlit `app.py`

### 1.1 사용자 입력 수신

| 위치 | 동작 |
|------|------|
| `app.py:34-45` (사이드바) | `st.text_input("주소")` + `st.button("📝 보고서 생성")` |
| `app.py:88-95` | 버튼 클릭 시 `_run_pipeline_with_progress(address)` 호출 |

```python
# app.py:69
def _run_pipeline_with_progress(address: str):
    with st.status(...):
        ...
        final = run_pipeline(address)   # ← src/graph.py::run
        ...
        return final
```

### 1.2 결과 영속화 후 재로드

```python
# app.py:88-97
final = _run_pipeline_with_progress(address_input.strip())
if final:
    report_md = final.get("report_markdown", "")
    kg_json   = final.pop("knowledge_graph", None)
    paths     = save_run(address_input.strip(), final, report_md, kg_json)
    st.session_state["loaded_run_id"] = Path(paths["dir"]).name
    st.rerun()                          # 새로고침 → 로드 분기로 진입
```

이후 `app.py:108-118`의 로드 분기에서 `QASession.load(...)`로 보고서/JSON/KG를 한꺼번에 읽어 4개 탭을 렌더링한다.

---

## 2. 파이프라인 진입점 — `src/graph.py::run(address)`

```python
def run(address: str) -> dict[str, Any]:
    _RUN_CACHE.clear()                  # 동일 프로세스 내 KG 상태 초기화
    graph = build_graph()               # LangGraph StateGraph 컴파일
    final = graph.invoke(
        {"address": address},
        config={"callbacks": _CALLBACKS, "run_name": "sto-valuation-v2"},
    )
    # KG 객체를 JSON dict로 직렬화해 final에 부착
    ...
    return final
```

### 2.1 상태 객체 — `src/state.py::PipelineState`

```python
class PipelineState(TypedDict, total=False):
    address: str
    metadata: dict      # Node 1 출력
    news: dict          # Node 2 출력
    esg: dict           # Node 3 출력
    awareness: dict     # Node 4 출력
    vitality: dict      # Node 5 출력
    composite: dict
    report_markdown: str
```

LangGraph는 각 노드의 반환 dict를 자동으로 이 TypedDict에 머지한다.

### 2.2 그래프 위상 — `build_graph()`

```
START → metadata → news ─┬─ esg       ─┐
                         ├─ awareness ─┤→ report → END
                         └─ vitality  ─┘
```

`news → {esg, awareness, vitality}`는 LangGraph가 **자동 병렬 dispatch** 한다(같은 부모를 갖는 노드들). `{esg, awareness, vitality} → report`는 모두 완료될 때 fan-in.

---

## 3. Node 1: 메타데이터 — `src/metadata/resolver.py`

### 3.1 진입

```python
# src/graph.py:36
def _node_metadata(state):
    md = resolve_address(state["address"])
    return {"metadata": md}
```

### 3.2 `resolve_address(address: str) → dict`

`src/metadata/resolver.py:9`. 다음 도구를 순차 호출:

| 단계 | 호출 | 파일 | 출력 |
|------|------|------|------|
| ① 도로명 → 정형 주소·PNU 후보 | `juso_tool.search_address` | `src/tools/juso.py:11` | `{top, candidates}` (행안부 API) |
| ② PNU 추출 | `juso_tool.pnu_from_juso_row` | `src/tools/juso.py:33` | 19자리 PNU |
| ③ PNU 분해 | `juso_tool.split_pnu` | `src/tools/juso.py:42` | `{pnu, sigungu_cd, bjdong_cd, land_type_code, bun, ji}` |
| ④ 주소 → 좌표 | `kakao_local.address_to_coord` | `src/tools/kakao_local.py:44` | `{x(경도), y(위도)}` |
| ⑤ 토지·공시지가 | `vworld_tool.land_pack` | `src/tools/vworld.py:21` | `{land_forest_ledger, land_characteristics, land_use, official_land_price}` |
| ⑥ 건축물대장 | `building_tool.building_pack` | `src/tools/building.py:35` | `{basis, title, recap_title, floor, zone, exclusive_section}` |

### 3.3 출력 형태

```jsonc
{
  "input_address": "서울특별시 강남구 테헤란로 152",
  "address": {
    "road": "서울특별시 강남구 테헤란로 152 (역삼동)",
    "jibun": "...",
    "sido": "서울특별시",
    "sigungu": "강남구",
    "bjdong_name": "역삼동",
    "building_name": "강남파이낸스센터",
    "building_management_no": "..."
  },
  "identifiers": { "pnu": "1168010100107370000", "sigungu_cd": "11680", ... },
  "coord": { "x": "127.0365...", "y": "37.5000..." },
  "land": { ... },
  "building": { ... }
}
```

이 dict가 이후 모든 노드의 **고정 컨텍스트**가 된다.

### 3.4 외부 의존 API & 환경변수

| API | env 변수 | 위치 |
|-----|----------|------|
| 행안부 도로명주소 | `JUSO_CONFIRM_KEY` | `tools/juso.py` |
| Kakao Local | `KAKAO_REST_API_KEY` | `tools/kakao_local.py` |
| VWorld | `VWORLD_API_KEY` / `VWORLD_DOMAIN` | `tools/vworld.py` |
| MOLIT 건축물대장 | `MOLIT_SERVICE_KEY` | `tools/building.py` |

`src/config.py`가 `.env`에서 모든 키를 로드해 모듈 변수로 노출.

---

## 4. Node 2: 뉴스 → 온톨로지 → 지식그래프 — `src/news/pipeline.py`

### 4.1 진입

```python
# src/graph.py:41
def _node_news(state):
    payload = run_news(state["metadata"], callbacks=_CALLBACKS)
    kg = payload.pop("graph")           # KG 객체 분리 (TypedDict는 JSON 친화여야 함)
    _RUN_CACHE["kg"] = kg               # 모듈 캐시에 저장 (이후 축 노드가 참조)
    return {"news": payload}
```

### 4.2 `pipeline.run(metadata) → dict`

`src/news/pipeline.py:21`. 3단계:

#### Step 1. 크롤 — `crawler.fetch_news`
- `src/news/crawler.py:48`
- `_candidate_queries(metadata)`가 **8개 후보 쿼리** 생성  
  (자산명 / 시군구+자산명 / "자산명 매각·임대·리모델링" / 도로명 / 지번 / "시군구 법정동 부동산")
- 각 쿼리로 `naver_search/news.json` 호출, `pubDate` 기반 12개월 컷오프, 링크 기반 중복 제거
- 최대 80건 리턴

#### Step 2. 태깅 — `kg.tagger.tag_articles`
- `src/kg/tagger.py:39`
- 기사 1건씩 `tag_article` 호출 → Claude (`AGENT_MODEL` = Haiku 4.5)에 `SYSTEM_PROMPT`(`src/kg/ontology.py`)와 함께 전달
- 출력 스키마:
  ```jsonc
  {
    "asset_relevance": "high|medium|low|none",
    "sentiment": "positive|neutral|negative",
    "entities": [{"id":"e1","type":"Asset|Organization|Event|Issue|...","name":"...","attrs":{}}],
    "relations": [{"src":"e1","rel":"OWNED_BY|HAS_EVENT|...","dst":"e2","evidence":"..."}],
    "axis_signals": [{"axis":"ESG|AWARENESS|VITALITY","polarity":"...","strength":1-5,"note":"..."}]
  }
  ```

#### Step 3. 그래프 적재 — `kg.graph_store.KnowledgeGraph.ingest_tagged`
- `src/kg/graph_store.py:55`
- 자산 노드 1개 보장 → 기사 노드 추가 → 엔티티 노드 upsert(`MENTIONED_IN` 엣지 자동) → 관계 추가 → `axis_signals`를 `AFFECTS_AXIS` 엣지로 자산-축 사이에 저장
- 노드 ID는 `md5(type::name.lower())[:16]`로 결정 → 중복 자동 병합

### 4.3 출력 형태 (`state["news"]`)

```jsonc
{
  "asset_context": {"asset_name":"강남파이낸스센터", "road":"...", "sigungu":"강남구", "pnu":"..."},
  "fetch": {"queries":[...], "total_unique": 145, "errors":[]},
  "tagged_count": 30,
  "graph_summary": {
    "nodes": 87, "edges": 148,
    "node_types": {"Asset":8,"NewsArticle":30,"Organization":16,"Event":21,...},
    "relation_types": {"MENTIONED_IN":105,"AFFECTS_AXIS":3,"HAS_EVENT":13,...}
  },
  "ingest_stats": {...}
}
```

KG **객체 자체**는 `_RUN_CACHE["kg"]`에 별도 보관(객체 그대로 보존). 이후 모든 축 노드와 report 노드가 동일 객체를 참조.

---

## 5. Node 3-5: 축 평가 서브그래프 — `src/agents/axis_subgraph.py`

세 축(`esg`/`awareness`/`vitality`)은 **동일 함수** `run_axis(...)`를 라벨만 바꿔 호출. LangGraph는 이 셋을 병렬 dispatch.

```python
# src/graph.py:48
def _node_esg(state):
    return {"esg": run_axis("ESG", "ESG (환경·사회·거버넌스)",
                            state["metadata"], _RUN_CACHE.get("kg"),
                            callbacks=_CALLBACKS)}
```

### 5.1 `run_axis(axis, axis_label, metadata, kg, callbacks)`

`src/agents/axis_subgraph.py:166`. 내부 3단계:

```
┌─────────────────────────────────────────────────────────┐
│ 1) Planner LLM   (Haiku)                                │
│    in : 축 라벨 + metadata 요약 + KG summary             │
│    out: {hypothesis, must_check:[{tool,args,why}], ...}  │
├─────────────────────────────────────────────────────────┤
│ 2) Retriever ReAct 루프  (Haiku + create_react_agent)    │
│    tools: 11개 (build_tools에서 metadata + KG 클로저)    │
│    동작: plan을 받아 도구를 자율 호출, 결과 보고 추가 호출│
│    출력: tool_calls 리스트 + 최종 자연어 코멘트          │
├─────────────────────────────────────────────────────────┤
│ 3) Scorer LLM    (Haiku)                                │
│    in : plan + tool_calls (각 최대 600자) + 최종 코멘트  │
│    out: {score, grade, strengths, weaknesses, risks,    │
│          rationale, citations}                          │
└─────────────────────────────────────────────────────────┘
```

### 5.2 도구 카탈로그 — `src/agents/tools_lc.py::build_tools(metadata, kg)`

11개의 LangChain `@tool` 래퍼. 클로저로 metadata와 KG를 캡쳐하므로 LLM은 인자만 결정한다.

| 도구 이름 | 호출 대상 | 용도 |
|-----------|----------|------|
| `get_subject_metadata` | metadata 압축 dict | 자산 기본 정보 |
| `gseed_lookup(address_keyword)` | `tools/gseed.py` | G-SEED 녹색건축 인증 |
| `naver_web_search(query)` | `tools/naver_search.py` | 6채널 통합검색 total |
| `naver_keyword_volume(hint)` | `tools/naver_searchad.py` | 월간 검색량 |
| `kakao_keyword_places(query, radius_m)` | `tools/kakao_local.py` | 키워드 POI |
| `kakao_poi_radius(radius_m)` | 동상 | 반경 카테고리 카운트 |
| `sbiz_stores_radius(radius_m)` | `tools/sbiz.py` | 소상공인 상권정보 |
| `molit_transactions(months, type)` | `tools/molit_trade.py` | 실거래가 |
| `graph_db_axis_evidence(axis, top_k)` | `KnowledgeGraph.axis_evidence` | 뉴스 축 시그널 |
| `graph_db_search(query)` | `KnowledgeGraph.search_entities` | 엔티티 검색 |
| `graph_db_neighbors(entity)` | `KnowledgeGraph.neighbors` | 이웃 관계 |

모든 호출은 `_safe(...)`로 감싸 예외를 `{error:..., message:...}` JSON으로 변환 → ReAct 루프 중단 방지.

### 5.3 ReAct 루프 동작

```python
# src/agents/axis_subgraph.py:106 (_retriever)
agent = create_react_agent(llm, tools)
result = agent.invoke(
    {"messages": [SystemMessage(...), HumanMessage(plan)]},
    config={"recursion_limit": 32, "run_name": f"retriever-{axis_label}"},
)
```

LangGraph `prebuilt.create_react_agent`가 ReAct 패턴(tool_call → tool_result → tool_call → ...)을 자동 구현. 평균 9~11회 도구 호출 후 종료. 모든 `ToolMessage`/`AIMessage`가 tool_calls 리스트에 누적되어 Scorer에 전달.

### 5.4 출력 (`state["esg" | "awareness" | "vitality"]`)

```jsonc
{
  "axis": "ESG",
  "axis_label": "ESG (환경·사회·거버넌스)",
  "plan": {"hypothesis": "...", "must_check": [{...}], "follow_up_hints":[]},
  "retrieved_summary": {"tool_call_count": 11, "iterations": 11, "final_text":"..."},
  "tool_calls": [{"tool":"...", "content":"..."}, ...],
  "score": 68,
  "grade": "B+",
  "strengths": [...],
  "weaknesses": [...],
  "risks": [...],
  "rationale": "...",
  "citations": [...]
}
```

---

## 6. Node 6: 보고서 합성 — `src/agents/report.py`

### 6.1 진입

```python
# src/graph.py:60
def _node_report(state):
    kg = _RUN_CACHE.get("kg")
    kg_insights = {
        "summary": kg.summary(),
        "axis_evidence": {
            "ESG": kg.axis_evidence("ESG"),
            "AWARENESS": kg.axis_evidence("AWARENESS"),
            "VITALITY": kg.axis_evidence("VITALITY"),
        },
    }
    enriched = {**state, "kg_insights": kg_insights}
    return report_agent.run(enriched, callbacks=_CALLBACKS)
```

### 6.2 `report.run(state) → {composite, report_markdown}`

`src/agents/report.py:45`. 두 단계:

1. **`_composite(axes)`** — 결정형 평균 계산 + 등급 매핑(`A+ ≥ 90 ... D < 40`)
2. **Claude 합성 호출** — `REPORT_MODEL`(기본 Sonnet 4.6)
   - SYSTEM 프롬프트: 9개 섹션 구조 강제(표지/자산개요/평가표/3축 상세/KG 종합/STO 의견/데이터 한계)
   - USER 페이로드: `{generated_at, address, metadata, axes, composite, news_summary, knowledge_graph}` JSON 12만 자까지 첨부
   - 출력: 마크다운 본문 한 덩어리

### 6.3 최종 `final` 객체

`graph.invoke`가 반환하는 상태 dict + `knowledge_graph`(JSON dict로 직렬화):

```jsonc
{
  "address": "...",
  "metadata": {...},
  "news": {...},
  "esg": {...},
  "awareness": {...},
  "vitality": {...},
  "composite": {"composite_score": 69.3, "grade": "B"},
  "report_markdown": "# 강남파이낸스센터 ...\n...",
  "knowledge_graph": {"directed": true, "nodes": [...], "links": [...]},
  "knowledge_graph_path": "data/knowledge_graph.json"
}
```

---

## 7. 영속화 — `src/web/run_store.py`

```python
# app.py:91-93
report_md = final.get("report_markdown", "")
kg_json   = final.pop("knowledge_graph", None)
paths     = save_run(address_input.strip(), final, report_md, kg_json)
```

`save_run` → `data/runs/<md5(address)[:12]>/`에 4개 파일 저장:
- `report.md`
- `result.json` (보고서 마크다운 + 3축 + composite + metadata + news 요약)
- `knowledge_graph.json` (NetworkX node_link 직렬화)
- `meta.json` (`{address, saved_at}`)

`list_runs()`로 사이드바 히스토리 표시, `load_run(id)`으로 경로 dict 복원.

---

## 8. UI 렌더링 — `app.py` 탭별

```python
# app.py:108-118
loaded = load_run(run_id)
session: QASession = QASession.load(loaded["report"], loaded["result"], loaded["kg"])
```

이후 4개 탭이 `session`(또는 `session.result`)을 읽어 표시.

| 탭 | 핵심 코드 | 데이터 소스 |
|----|----------|-------------|
| 📄 보고서 | `app.py:139-145` | `session.report_text` (그대로 마크다운 렌더) |
| 🔬 평가 상세 | `app.py:148-180` | `session.result["esg"|"awareness"|"vitality"]` → 표 + plan/tool_calls expander |
| 🕸 지식그래프 | `app.py:183-225` | `session.result["news"]` + `session.kg.axis_evidence(axis)` |
| 💬 QA 챗봇 | `app.py:228-282` | `qa.graph.answer(question, session)` |

---

## 9. QA 흐름 (Tab 4) — `src/qa/graph.py::answer`

사용자가 챗 입력창에 질문을 치면:

```python
# app.py:266-272
result_qa = qa_answer(question, st.session_state["qa_session"])
```

### 9.1 `answer(question, session) → dict`

`src/qa/graph.py:30`. 3단계:

#### Step 1. Router — `src/qa/router.py::route`
- Claude(Haiku)에 질문 + 자산 + 대화 히스토리 전달
- 출력 JSON: `{specialists:[...], parallel:bool, reasoning:str}`
- LLM 실패 시 키워드 휴리스틱 폴백(`_heuristic`)

#### Step 2. 전문가 fan-out — `ThreadPoolExecutor` 병렬
선택된 specialist 함수만 동시 실행. 모듈 매핑:

```python
# src/qa/graph.py:23
_SPECIALIST_FNS = {
    "report_analyst":   report_analyst.run,    # 보고서 본문 인용·요약
    "graph_query":      graph_query.run,       # KG 엔티티/관계/축 시그널
    "axis_deep_dive":   axis_deep_dive.run,    # 특정 축의 plan/tool_calls/rationale
    "live_tool":        live_tool.run,         # ReAct 라이브 도구 (build_tools 재사용)
    "composite_recalc": composite_recalc.run,  # 가중치 시뮬레이션 (계산형 + LLM 해설)
    "fallback":         fallback.run,
}
```

각 전문가의 컨텍스트 데이터 소스:

| 전문가 | 읽는 데이터 |
|--------|-------------|
| `report_analyst` | `session.report_text` |
| `graph_query` | `session.kg` (search/neighbors/axis_evidence) |
| `axis_deep_dive` | `session.axis("esg"|...)` — plan + tool_calls + rationale |
| `live_tool` | `build_tools(session.metadata(), session.kg)` 새 ReAct 루프 |
| `composite_recalc` | `session.composite()` + 정규식으로 질문에서 가중치 파싱 |
| `fallback` | 자산 요약만 |

#### Step 3. Synthesizer — `src/qa/synthesizer.py::synthesize`
- 전문가가 1명이면 그대로 통과
- 2명 이상이면 Claude(Haiku)가 중복·모순 정리 후 단일 마크다운 응답 생성
- 결과는 `session.history`에 append(다음 턴 컨텍스트로 사용)

### 9.2 출력 형태

```jsonc
{
  "answer": "최종 마크다운 답변",
  "routing": {"specialists": ["axis_deep_dive"], "parallel": true, "reasoning": "..."},
  "specialist_answers": {"axis_deep_dive": "..."}
}
```

UI는 `answer`를 chat_message에 출력, `routing`·`specialist_answers`는 expander로 디버그 노출.

---

## 10. 트레이싱 — `src/observability.py`

```python
# src/observability.py:25
def langchain_callbacks() -> list:
    from langfuse.langchain import CallbackHandler
    return [CallbackHandler()]
```

이 콜백 리스트가 다음 호출 경로 전체에 전파:
- `src/graph.py::run` → 전체 파이프라인 span
- 각 노드 함수가 callbacks를 LLM `.invoke(config={"callbacks":...})`에 전달
- `create_react_agent` 호출에도 동일하게 전달

결과적으로 Langfuse 대시보드에 다음 트레이스가 생성된다:
- `sto-valuation-v2` (전체)
  - `metadata`, `news`, `esg`, `awareness`, `vitality`, `report` 노드 span
    - 각 축 노드 안에 `retriever-ESG` 등 ReAct 서브트레이스
      - tool_call 별 span (각 도구 호출 시간/입출력)
- QA는 별도 트레이스: Router LLM → 전문가 LLM(병렬) → Synthesizer LLM

---

## 11. 모델·역할 매핑

`src/config.py`:

| 역할 | 모델 변수 | 기본값 | 사용처 |
|------|----------|--------|--------|
| 보고서 최종 합성 | `REPORT_MODEL` | `claude-sonnet-4-6` | `src/agents/report.py` |
| Planner / Retriever / Scorer / QA | `AGENT_MODEL` | `claude-haiku-4-5-20251001` | `src/agents/_llm.py::make_llm()` 기본값 |

`make_llm`이 `opus-4-7` 모델은 `temperature` 인자를 자동으로 제거(API 호환성).

---

## 12. 외부 환경변수 전체 목록 — `.env`

| 키 | 사용처 |
|----|--------|
| `JUSO_CONFIRM_KEY` | 행안부 도로명주소 |
| `MOLIT_SERVICE_KEY` / `DATA_GO_KR_SERVICE_KEY` | 건축물대장, 실거래가, SBIZ |
| `VWORLD_API_KEY` / `VWORLD_DOMAIN` | 토지·공시지가 |
| `SERVICE_KEY` | G-SEED odcloud |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 통합검색, 뉴스 |
| `CUSTOMER_ID` / `ACCESS_LICENSE` / `SECRET_KEY` | 네이버 SearchAd |
| `KAKAO_REST_API_KEY` | 카카오 로컬 |
| `SBIZ_SERVICE_KEY` | 소상공인 상권 |
| `ANTHROPIC_API_KEY` | Claude 호출 전체 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | Langfuse 트레이싱 |
| (옵션) `REPORT_MODEL` / `AGENT_MODEL` | 모델 오버라이드 |

---

## 13. 단일 입력의 전체 데이터 흐름 (요약 표)

| 순번 | 호출 | 입력 | 출력 변수 |
|------|------|------|----------|
| 1 | `app.py` UI | "서울특별시 강남구 테헤란로 152" | `address` |
| 2 | `src/graph.py::run(address)` | address | LangGraph state 초기화 |
| 3 | `_node_metadata` → `metadata.resolver.resolve_address` | address | `state["metadata"]` |
| 4 | `_node_news` → `news.pipeline.run` | metadata | `state["news"]` + `_RUN_CACHE["kg"]` |
| 5a | `_node_esg` → `axis_subgraph.run_axis("ESG", ...)` | metadata, kg | `state["esg"]` |
| 5b | `_node_awareness` → `run_axis("AWARENESS", ...)` | 동상 | `state["awareness"]` |
| 5c | `_node_vitality` → `run_axis("VITALITY", ...)` | 동상 | `state["vitality"]` |
| 6 | `_node_report` → `agents.report.run` | state+kg_insights | `state["composite"]`, `state["report_markdown"]` |
| 7 | `src/graph.py::run` 후처리 | state + KG | `final` dict 반환 (knowledge_graph JSON 부착) |
| 8 | `app.py` → `web.run_store.save_run` | final, report_md, kg_json | `data/runs/<hash>/` 디렉토리 |
| 9 | `app.py` → `QASession.load` | 위 디렉토리 경로들 | `session` 객체 |
| 10 | 사용자 질문 → `qa.graph.answer` | question, session | `{answer, routing, specialist_answers}` |

---

## 14. 변경 시 영향도 가이드

| 변경하고 싶은 것 | 수정 위치 |
|----------------|----------|
| 새 데이터 소스 추가 | `src/tools/<new>.py` 작성 → `src/agents/tools_lc.py`에 `@tool` 추가 → 플래너 SYSTEM 프롬프트에 힌트 추가 |
| 가중치/등급 임계 | `src/agents/report.py::_composite`, `src/qa/specialists/composite_recalc.py::_grade_for` |
| 뉴스 기간/쿼리 | `src/news/crawler.py::fetch_news` (months) + `_candidate_queries` |
| 온톨로지 클래스/관계 | `src/kg/ontology.py` (SYSTEM_PROMPT + ENTITY_TYPES) |
| QA 전문가 추가 | `src/qa/specialists/<new>.py` 작성 → `src/qa/graph.py::_SPECIALIST_FNS` 등록 → `router.py::SPECIALISTS` 추가 |
| 모델 변경 | `.env`의 `REPORT_MODEL` / `AGENT_MODEL` |
| 그래프 DB 백엔드 | `src/kg/graph_store.py::KnowledgeGraph` 만 교체(인터페이스: `upsert_entity`, `add_relation`, `axis_evidence`, `search_entities`, `neighbors`, `summary`, `save/load`) |
