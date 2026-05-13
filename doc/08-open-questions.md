# 08. Open Questions

아직 합의되지 않았거나, 다음 자산이 등장하면 다시 들여다봐야 할 토픽들.
각 항목은 *왜 지금은 결정 안 했나*, *언제 다시 봐야 하나*, *후보 선택지*.

---

## Q1 · 음악 자산의 도메인 모델

**상태**: 미정 (현 placeholder 단계).

**왜 지금은 결정 안 했나**: 음악 저작권 시장의 평가 입력(스트리밍 차트, 정산
데이터, 저작권 분쟁 뉴스 등)을 깊게 조사하지 않았음.

**언제**: 음악 자산 실 구현 착수 시점.

**그때 검토할 것**:
- `SongTarget`, `ArtistTarget` 외 추가 단위(앨범? 퍼블리싱 권리?) 가 필요한가?
- 부동산의 6축 ontology(Society/Policy/Tenant/Market/Macro/Local) 가 음악에
  맞는가? 안 맞으면 자산별 enum 으로 가르고, *겹치는 축만* `common/domain` 으로
  승격할지 결정.
- `EvaluationFactor` 사전: 스트리밍 추세, 저작권 분쟁, 아티스트 활동, 플랫폼 정산.
- 외부 데이터 source: Spotify/Apple charts, KOMCA 정산, 음악 산업 뉴스.

---

## Q2 · 그래프 저장소의 실 구현

**상태**: in-memory stub.

**왜 지금은 결정 안 했나**: 본 세션 범위 밖. on-demand 그래프(scope_id 격리)와
배치 누적 그래프가 같은 store 를 쓸지/분리할지도 미정.

**후보**:
- Neo4j (cypher 친화, 운영 부담)
- Postgres + Apache AGE (단일 DB로 운영 단순화)
- DuckDB + custom graph layer (소규모/로컬 분석)
- 단계적: in-memory → 단일 RDB(테이블 2개) → 그래프 DB

**제약 사항**:
- `scope_id` 로 노드/엣지를 격리할 수 있어야 함 (on-demand 요청 간 비파괴).
- traversal 패턴이 단순 (1-2 hop) — 그래프 DB 까지 안 가도 됨.

---

## Q3 · 벡터 저장 / 의미 dedup

**상태**: cosine 함수만 stub, 벡터 store 없음.

**왜 지금은 결정 안 했나**: 1차는 임베딩을 메모리에서 비교해도 충분.
일별 뉴스 수가 적기 때문.

**언제**: 누적 뉴스가 N만 건을 넘어 메모리 비교가 비현실적이 되는 시점.

**후보**: pgvector, Qdrant, Chroma, LanceDB.

---

## Q4 · 외부 API 비용·쿼터 store 의 분산화

**상태**: in-memory.

**언제**: 워커가 1대를 넘어 quota/cost 가 글로벌하게 합산되어야 할 때.

**대안**: redis (TTL=하루) — `common/ports/` 에 `CostStore`, `QuotaStore`
인터페이스를 추가하고 in-memory 와 redis 를 plug-in.

---

## Q5 · LangGraph 상태 영속화

**상태**: 미고려.

**컨텍스트**: on-demand 분석이 길어질 경우 중간 상태를 persistent 하게 두고
이어 갈 수 있는지.

**후보**: LangGraph 의 `MemorySaver`/`PostgresSaver` 채택, 또는 안 함 (1회 요청 내
완결).

---

## Q6 · MCP 활성화 시점·범위

**상태**: stub.

**검토 사항**:
- 자산 도구를 *내부* 에이전트만 쓸 거라면 MCP 불필요 (직접 `BaseTool` 바인딩).
- *외부* 에이전트(다른 시스템·다른 모델)와 도구 공유가 목표면 MCP 가치 있음.
- 외부 MCP 서버(예: 공개 데이터 MCP) 를 *소비* 할 거라면 `common/mcp/client.py` 가
  먼저 구현됨.

**현재 가설**: 1차는 직접 바인딩만으로 충분. MCP 는 외부 시스템 연동 요구가
나타날 때.

---

## Q7 · API 백엔드 / 웹 UI

**상태**: 본 프로젝트 범위 밖.

**컨텍스트**: product.md §5.2 에 "웹 UI 로 만들고, API 백엔드를 연결할 계획"
명시. 본 저장소는 도메인/엔진 레이어이고 UI/API 는 별도 저장소가 될 가능성.

**검토 사항**: 본 패키지를 `pip install -e .` 가능한 라이브러리로 유지해 API
서비스에서 import. CLI 와 API 가 같은 `composition.resolve(...)` 를 호출하는
구조 권장.

---

## Q8 · OntologyMapper 의 위치/이름

**상태**: product.md §5.2 에 "OntologyMapper (adapter 직접 매핑 또는 LLM
fallback)" 등장. 현재 코드에는 명시적 모듈 없음 — `OntologyTagger` 와
`GraphSignalWriter` 로 분리되어 있음.

**검토 사항**:
- 두 책임을 하나로 합치는 것이 product.md 의 의도였는지?
- 분리 형태가 맞다면 이름을 product.md 와 일치시킬지 (예: `ontology_mapper.py`).

---

## Q9 · 평가 3축 중 정량·구조의 외부 자료

**상태**: `score=0.0` 고정 stub.

**컨텍스트**: 본 프로젝트는 감정축 집중. 정량/구조는 "기존 방식 차용". 외부
모듈/라이브러리/회사 내부 평가 엔진이 있다면 그것을 호출하는 어댑터만 두면 됨.

**검토 사항**: 외부 평가 엔진의 위치와 API 가 정해질 때 어댑터 design.

---

## Q10 · 자동 의존 그래프 검증 도구

**상태**: 문서로만 명시.

**도입 후보**:
- `import-linter` — `.importlinter` 설정으로 레이어 규칙 강제.
- `ruff TID` — banned-imports 룰.

**언제**: 코드가 본격적으로 채워질 때 (지금은 시그니처만이라 위반 가능성 낮음).
