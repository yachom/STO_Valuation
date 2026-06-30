# 엔티티 해소의 2계층 구조 (Entity Resolution Layers)

> 작성일: 2026-06-30 · 대상: `music_copyright` graphflow · 브랜치: `yachom/feature/music-copyright`
> 검증 케이스: RESCENE("리센느") "LOVE ATTACK" (2024) · 관련 커밋 `4e13dcc`, `4c1fa7d`
> 선행 문서: [entity_resolution_gap.md](entity_resolution_gap.md) (agentic_graph 도입 *이전*의 갭 진단)

## TL;DR

수집된 표면형(mention)이 평가 대상(Work/Artist)에 연결되는 경로는 **서로 다른 두 계층**이다.
이 둘을 혼동하면 "왜 별칭(괄호·한글·영어)을 LLM이 못 알아봤지?" 같은 잘못된 질문을 하게 된다.

| | **A. 적재-시점 mention 링킹** | **B. 사후 정리(proposer)** |
|---|---|---|
| 위치 | `harvester._link_mentions` | `agentic_graph` |
| 방식 | **결정적 코드(문자열)** — LLM 아님 | **LLM 자율 제안** + 코드 validator |
| 모델 | 없음 | `music_graph_proposer` = gpt-4o-mini |
| 입력에 기존 엔티티 목록? | **불필요**(스코프 이름 인덱스를 코드가 직접 매칭) | **있음**(`existing_scope_refs` + 스키마) |
| 만드는 것 | `Fact -MENTIONS-> {기존 노드}` 엣지 | 고아 `Entity`의 **승격/dedup/관계** |
| 별칭·괄호·한글/영어 처리 책임 | **여기**(`_match_scope_name`) | (해당 없음) |

**핵심**: 괄호/원어-로마자 표기 매칭은 **A(결정적 코드)의 책임**이고, 거기엔 프롬프트도 모델도 없다.
"리센느(RESCENE)" 미매칭은 LLM의 인식 실패가 아니라 **문자열 동등성의 한계**였고, 커밋
`4e13dcc`/`4c1fa7d`가 바로 그 코드 계층을 고친 것이다.

---

## 1. 두 계층은 무엇을, 어떤 정보로 하는가

### A. 적재-시점 mention 링킹 — 결정적, LLM 아님

harvester의 추출 LLM(`music_harvester`, gpt-4o-mini)은 문서에서 Fact와 **mention 표면형**만
뽑는다. **어느 기존 노드에 붙일지는 결정하지 않는다.** 추출 LLM이 받는 payload는 평가 카드 +
검색어 + 문서뿐이며 **기존 엔티티 목록을 받지 않는다**:

```
# harvester.py:752  _ingest_doc
user = f"[평가 대상]\n{work_card}\n\n[검색어]\n{query}\n\n[문서] {title}\nURL ...\n{content}"
```

표면형 → 기존 노드 링킹은 그 직후 **코드**가 한다:

```
harvester.py:824  self._link_mentions(s, fact_id, fd.mentions, scope_names, ...)
harvester.py:889  _link_mentions:
    matched = _match_scope_name(normalize_text(m.surface), scope_names)
    if matched:  Fact -MENTIONS-> (label,key)        # 기존 노드에 연결
    else:        Fact -MENTIONS-> Entity(고아)         # 미해소 잔여
```

- `_load_scope_names`/`_index_scope_names`(harvester.py:221) — 스코프 노드의 이름 + **음차 별칭
  (`Work.title_aliases`·`Artist.artist_aliases`)** 을 `{정규화 표면형: (label,key)}`로 인덱싱.
  이 별칭은 시더가 **MusicBrainz에서 데이터로** 가져온 것(하드코딩 사전 아님).
- `_match_scope_name`(harvester.py:261) — 정규화 정확일치 → **괄호부 제거 후 정확일치**(`_PAREN_RE`,
  "리센느(rescene)"→"리센느") → 가드된 부분일치(짧은 이름·길이비율<0.6 차단).

**코드는 추론을 못 한다.** 그래서 `"리센느" == "RESCENE"`를 알게 하려면 별칭을 인덱스에 넣어야
했고(`4e13dcc`), `"리센느(RESCENE)"` 합성 표면형은 괄호를 떼어 흡수해야 했다(`4c1fa7d`).
이것이 **별칭/괄호 처리가 코드인 이유**다 — 프롬프트·모델과 무관.

### B. 사후 정리(proposer) — LLM 자율 + validator

A에서 매칭 못 한 표면형은 고아 `Entity`로 남는다. `agentic_graph`의 `music_graph_proposer`
(gpt-4o-mini)가 이 잔여를 **(타입 → 관계 → 앵커)** 로 판단해 제안하고, 코드 validator가
스키마·근거·앵커를 검증한 뒤 커밋한다.

이 LLM은 **기존 엔티티 정보를 받는다**(A와의 결정적 차이):

```
# agentic_graph.py:179  GraphProposalAgent.propose payload
"existing_scope_refs":    state.scope_refs,            # 스코프 내 기존 노드 label:key
"unresolved_entities":    [고아 Entity + 각 Fact + work_anchored],
"allowed_relationships":  {rel: {src, dst} ...},       # 엣지별 (src→dst) 스키마 명시
"allowed_candidate_labels": [...],
```

action: `promote_entity`(승격+관계) / `same_as_candidate`(기존 노드와 동일) /
`mark_review`(불확실 → 사람) / `reject`(노이즈). 자동 커밋은 **객관 엣지
`CHARTED_ON/WON/CERTIFIED/HAS_FANDOM`만**(하드 실링), 해석적 엣지는 review 격리.
validator는 품질점수가 아니라 불변식만 본다: 허용 엣지인가 · 스키마상 합법 (src→dst)인가
(`EDGE_ENDPOINTS`) · 근거 Fact가 실재+`source_excerpt`인가 · 작품에 구조적으로 앵커됐나.

---

## 2. 두 계층이 *모두* 필요한 이유

"proposer LLM이 기존 엔티티를 알고 해소하는데, 왜 A의 문자열 매칭이 또 필요한가?"

1. **결과물이 다르다.** A는 적재 시점에 **`Fact -MENTIONS-> Artist` 엣지**를 만든다 —
   검색·탐색(scope_eids, retrieval) 연결의 근간. B는 MENTIONS를 만들지 않고 고아 *Entity*를
   승격/dedup할 뿐이다. A가 "리센느"를 못 붙이면 그 Fact의 아티스트 연결이 끊기고, B는 끝에
   한 번 capped(`max_entities`/`max_proposals`)로 돌며 해석적·불법 케이스를 review로 보내므로
   **자동 복구가 보장되지 않는다.**
2. **비용/정밀도.** 알려진 별칭 정확일치는 공짜·100% 정밀·mention마다 즉시. 모든 표면형 해소를
   LLM에 넘기면 느리고 확률적이다. 올바른 분업 = **싼 결정적 매칭이 쉬운(원어↔로마자) 케이스를
   적재 시점에 잡고, LLM은 진짜 모호한 잔여만** 풀 컨텍스트로 처리.
3. **자율성 원칙**([music-graph-autonomy-principle] 메모리). A의 별칭은 하드코딩 사전이 아니라
   시더가 MB에서 가져온 데이터를 읽고 괄호를 일반 규칙으로 떼는 것이라 원칙과 합치.

---

## 3. 진단 — proposer는 무엇을, 왜 실패했나 (그리고 안 했나)

흔한 오해: "proposer가 괄호를 인식 못 했다." → **사실이 아니다.** 괄호 미매칭은 A(코드)의 일이고,
proposer 로그상 "리센느"는 **기존 Artist로 정확히 인식**됐다(`existing_ref` 정확). proposer에서
실제로 일어난 별개 사건은 **불법 엣지 제안**이었다:

- `HAS_FANDOM`의 스키마는 `(Artist) → (Fandom)` (schema.py:114). 이 src/dst는 **프롬프트의
  `allowed_relationships`에 명시돼 전달된다.** 그런데도 dst가 Fandom이 아닌 제안이 나왔고 →
  validator가 스키마 위반으로 거부 → review_candidate. **즉 프롬프트에 정보가 없어서가 아니라,
  명시된 제약을 모델이 안 지킨 것.**

따라서 사용자의 두 갈래 질문에 답하면:

- **"괄호 미인식이 프롬프트/모델 버전 문제?"** → 아니다. 괄호는 A(LLM 없는 코드)의 일이라 프롬프트·
  모델 자체가 존재하지 않는다. 이미 코드로 정타 수정됨.
- **"여러 엔티티로 프롬프트가 길어져 정확도가 떨어진 것?"** → 그것이 proposer 불법-엣지의 *근본*은
  아니다. 스키마는 이미 프롬프트에 있으므로 근본 원인은 **모델 능력**(gpt-4o-mini가 다중-엔티티
  스키마 제약 추론에 약함)이다. 다만 payload 규모(`max_entities=24` × `facts_per_entity=4` +
  관계 스키마)가 작은 모델의 정확도를 **부차적으로** 떨어뜨릴 수는 있다(주원인 아님, 가중요인).

### 그럼 proposer를 건드려야 하나?

- **리센느/괄호 케이스 한정: 아니오.** A의 수정으로 고아가 애초에 안 생긴다(검증: '리센느' 고아
  0건). proposer는 이제 그 표면형을 보지도 않는다.
- **일반 fallback 견고화**(버그 수정이 아니라 강화 — 현재도 불법 제안은 validator가 review로 안전
  격리해 그래프를 오염시키지 않는다):
  - **(a) 모델 상향 — 구현됨(2026-06-30).** proposer를 `gpt_mini`(gpt-4o-mini)→**중간 티어
    `gpt_54_mini`(gpt-5.4-mini)** 로 (`config/models.json`). 4o-mini↔5.4 사이의 단계적 상향 —
    스키마 준수·다중-엔티티 해소 정확도는 올리되 최상위(gpt_54=writer/synthesizer) 비용은 피한다.
  - **(c) same_as 구제(decouple) — 구현됨(2026-06-30).** proposer가 "이 고아 = 기존 노드"는
    맞게 봤는데(유효한 `existing_ref`) 덧붙인 관계만 불법이면, 엣지는 버리되 **dedup(same_as)은
    살린다**(`apply_graph_proposals` promote 실패 분기, `agentic_graph.py:326`). 엔티티 정체성은
    관계 적법성과 독립적이라 고아가 review로 방치돼 중복되는 걸 막는다. 회귀:
    `test_graph_proposal.py::SalvageSameAsTest`(3건). 스코프 한정: **LLM의 `existing_ref`
    판단**만 살린다(이름/별칭 결정적 매칭은 A 계층의 일이라 중복 구현 안 함).
  - (b) **payload 축소/구조화**(미구현, 선택) — 엔티티당 fact 수·`max_entities` 조정, 관계 스키마
    압축. 현재 동작이 안전하고 A 계층이 대부분의 음차 케이스를 이미 흡수하므로 비시급.

---

## 부록 — 관련 코드 위치

- 적재 mention 링킹: `harvester.py:889`(`_link_mentions`), `:261`(`_match_scope_name`),
  `:221`(`_index_scope_names`), `:218`(`_SCOPE_ALIAS_FIELDS`)
- 추출 LLM payload(기존 엔티티 없음): `harvester.py:752`
- proposer: `agentic_graph.py:179`(`propose` payload), `:151`(`GraphProposal`),
  `:55`(`_AUTO_COMMIT_EDGES`), `:587`(`_validate_promotion`)
- 엣지 스키마: `graph/schema.py:109`(`EDGE_ENDPOINTS`), `:114`(`HAS_FANDOM = Artist→Fandom`)
- 모델 배정: `config/models.json`(`music_harvester`·`music_graph_proposer` = `gpt_mini`)
- 튜닝: `config/music_graphflow.json` → `agentic_graph`(`max_entities`·`facts_per_entity`·
  `max_proposals`·`auto_commit_edges`)
