# 새 기초자산 추가 가이드 (STO 정성 가치 평가)

> 목적: 임의의 STO 기초자산(음악저작권·부동산·미술품·특허·향후 N종)을 **하나의 공통
> 파이프라인**으로 평가한다. 이 문서는 새 자산을 붙일 때 **무엇을 재사용하고 무엇을 새로
> 만드는지**, 그리고 **정형 데이터/외부 API를 어느 타이밍에 합치는지**를 정리한다.

## 0. 대전제 — 3계층

```
 infra/            공통 인프라 (자산 무관)  ── 절대 자산 지식 없음
   agent/          LLM 티어·complete·tracing·역할 베이스(BaseAgent/BaseSynthesizerAgent)
   graph/          Neo4j driver·walk·embedder·vector_index·viz(그래프 시각화)
   tool/ external/ 웹검색(ddgs·tavily·naver)·본문추출 — 인터넷 탐색은 공통 인프라
   gateway/ notify/ config_loader/ env/ asset/
   ─────────────────────────────────────────────────────────────
 <asset>/          자산 패키지 (music_copyright/, real_estate/, …)
   graphflow/      평가 엔진 — 현재 자산마다 **미러링**(같은 파일 이름, 도메인 어휘만 교체)
   external/       그 자산의 **정형 커넥터**(음악: musicbrainz·spotify·genius…; 부동산: kakao·juso·gov)
   resolver.py     식별자 해소(음악=WorkResolver, 부동산=LocationResolver)
```

**원칙**: *인터넷 탐색 = infra 공통 / 정형 수집 = 자산 하위 / 평가 엔진 = 미러링(→ 장기적으로
공통 엔진 + 자산 DomainPack 주입으로 수렴)*. 새 자산은 "엔진을 복제하고 도메인 어휘를 갈아끼운다".

---

## 1. 파이프라인 4단계 — 공통 vs 자산별

| 단계 | 하는 일 | 공통? | 자산이 정의하는 것 |
|---|---|---|---|
| ① **식별(resolve)** | 자유 입력 → 정규 자산 ID | ✗ 자산별 | `resolver.py` (음악: 제목+가수→ISWC/ISRC, 부동산: 주소→건물관리번호) |
| ② **시드 적재(seed)** | **정형 데이터**로 typed 노드 **백본** 구축 | 반반 | `seeder.py`(자산 커넥터 호출) + `graph/schema.py`(온톨로지) — 쓰기 자체는 공통 ScopeGraph |
| ③ **확장(expand)** | 웹검색→fact 추출→그래프 적재→엔티티 해소 | **✓ 엔진 공통** | frontier 라벨·`profiles`(엣지 가중치)·`axes`(질의 의도) |
| ④ **보고서(report)** | 축별 회수→집필→종합→등급 | **✓ 엔진 공통** | `axes`·프롬프트·등급 루브릭 |

→ **③④는 엔진(navigator·harvester·composer·rerank·writer·synthesizer·expand·agentic_graph·
consolidate)이 그대로.** 자산은 주로 **①②(식별·정형 백본) + 축·프로파일·프롬프트·스키마**만 정의한다.

---

## 2. 정형 데이터 / 외부 API 타이밍 ★(핵심 설계 결정)

**규칙: 정형(seed-time, 자산별) = *백본* / 웹(expand-time, 공통) = *살붙임*.**

```
resolve → seed_from_identity(자산 커넥터들: API·정형 DB) → typed 백본 노드
                     │  (음악: Spotify/MusicBrainz/Genius/Soundcharts/Last.fm/Wikidata)
                     │  (부동산: Kakao/Juso + 공공데이터 API — *웹검색으론 안 나오는 정형*)
                     ▼
        run_expand(웹검색·공통) → fact를 백본에 MENTIONS로 매달고, 엔티티를 typed로 승격
```

왜 이 순서인가:
1. **정형이 먼저 백본을 세워야** 웹 fact가 "무엇에 대한" 것인지 앵커링된다(work-anchoring·scope).
   백본 없이 웹만 긁으면 미해소 Entity가 떠다닌다.
2. **정형 소스는 자산마다 다르고 웹에 없을 수 있다** — 특히 부동산(등기·실거래·인허가 API)은
   웹검색으로 안 나온다. 그래서 **seed-time에 자산 커넥터로 직접 적재**해야 한다.
3. 웹(expand)은 **공통 인프라**(ddgs·naver·tavily)라 자산이 건드릴 일이 없다.

**새 자산 체크**: "이 자산의 평가 근거 중 *웹검색으로 안 나오는 정형 API*가 무엇인가?"
→ 그 커넥터를 `<asset>/external/`에 만들고 `seeder.py`가 seed-time에 호출하게 한다.
→ 자주 안 나오는 개방형 엔티티(수상·논란·파트너)는 seed에 넣지 말고 **expand+agentic_graph**에
   맡긴다(하드코딩 금지 원칙 — `music-graph-autonomy-principle`).

**주입 seam**: `seed_from_identity(identity, driver, *, <커넥터 키들>)` 시그니처에 자산 커넥터
자격증명을 넘긴다. 커넥터는 `<asset>/external/<source>/client.py`(음악 6종 참고: BaseExternalClient
상속 → audit·gateway 자동).

---

## 3. 새 자산 추가 — 파일 체크리스트

### A. 새로 만든다 (자산 지식)
| 파일 | 역할 | 참고(음악) |
|---|---|---|
| `<asset>/resolver.py` | 자유 입력 → 정규 ID (값 객체 + Resolver) | `WorkResolver`/`WorkIdentity` |
| `<asset>/external/<source>/client.py` | **정형 커넥터**(자산별 API·DB), `BaseExternalClient` 상속 | musicbrainz·genius·soundcharts… |
| `<asset>/graphflow/graph/schema.py` | **온톨로지**: NODE_LABELS·EDGE_TYPES·EDGE_ENDPOINTS·KEY_PROP | Work/Artist/Rightsholder… + BY_ARTIST… |
| `<asset>/graphflow/seeder.py` | 커넥터 호출 → ScopeGraph에 백본 적재 | `seed_from_identity` |
| `<asset>/graphflow/axes.py` | 평가 축(BASE_AXES: key·label·intent·profile) | 영향력·대중참여·지속성·사회공헌·혁신·ESG |
| `<asset>/graphflow/profiles.py` | 축별 엣지 가중치(PROFILES) | 축마다 walk 우선순위 |
| `<asset>/graphflow/coverage.py` | 축별 척추 라벨(`_AXIS_LABELS`) | 축→어느 노드가 근거인가 |
| `config/<asset>_prompts.json` | 자산 프롬프트(navigator·harvester·writer·synthesizer·reranker·agency_classifier·agentic proposer) | `music_prompts.json` |
| `config/<asset>_graphflow.json` | expand·retrieval·agentic_graph·consolidate 설정 | `music_graphflow.json` |
| `config/models.json` | 에이전트→티어 매핑 추가 | `music_harvester: gpt_mini`… |

### B. 미러링한다 (엔진 — 파일명 동일, 도메인 어휘만 교체)
`composer.py · harvester.py · navigator.py · rerank.py · research.py · writer.py ·
synthesizer.py · auditor.py · expand.py · report.py · workflow.py · agentic_graph.py ·
consolidate.py · routing.py · ppr.py · traversal.py · policies.py · prompts.py · config.py`
→ 로직은 자산 무관. **작품/주소 같은 도메인 명칭·라벨 참조만 교체.** (장기적으로 이 미러링을
   공통 엔진 + 자산 DomainPack 주입으로 합치는 게 리팩터링 방향 — `asset-common-pipeline-design`.)

### C. 그대로 재사용 (infra — 수정 0)
`infra/agent`(complete·티어·역할 베이스) · `infra/graph`(driver·walk·embedder·vector_index·
**viz** 그래프 시각화) · `infra/tool`·`infra/external`(ddgs·tavily·naver 웹검색 — *인터넷 탐색 공통*)
· `infra/gateway`·`infra/notify`·`infra/config_loader`·`infra/env`.

### D. UI (선택)
`scripts/app.py`(보고서)·`app2.py`(지식그래프)는 자산 토글로 이미 다자산. 새 자산은 드라이버·
resolve·seed·generate 어댑터 함수만 추가(기존 음악/부동산 어댑터 패턴 복제). 그래프 뷰(`infra/graph/viz`)·
근거 팝오버·N/A 등급 렌더는 **보고서 JSON 스키마가 공통이라 그대로** 동작.

---

## 4. 순서 (권장)
1. `resolver.py` — 식별부터(파이프라인 입구).
2. `schema.py` — 온톨로지(백본 노드·엣지) 확정.
3. `external/*` 커넥터 + `seeder.py` — **정형 백본 seed-time 적재**(§2).
4. `axes.py`·`profiles.py`·`coverage.py` — 평가 축.
5. 엔진 미러링(B) + 프롬프트·config(A).
6. 스모크: resolve→seed→expand(resolve=True) 1곡/1건 → `app2` 거미줄로 백본 확인 → 보고서.

## 5. 공통 원칙 (모든 자산 불변)
- **하드코딩 토큰/사전 금지** — 분류·승격은 LLM+validator, 불확실하면 review(`music-graph-autonomy-principle`).
- **정형=백본(seed)·웹=살붙임(expand)**, 엔티티 해소는 per-round(`resolve=True`)로 그래프를 체인 확장.
- **firewall**: 재무·상업 수치로 정성 가치를 대신 매기지 않음.
- **N/A 정직 등급**: 근거 0인 축은 가짜 등급 대신 N/A.
- 산출은 **정성 등급 + 근거 인용**(정량 금액 금지), 축-구동.
