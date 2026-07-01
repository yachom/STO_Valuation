# 엔티티 해소·승격 계층 — 구현 계획 (Implementation Plan)

> 작성일: 2026-06-25 · 상태: **✅ 구현 완료(2026-07-01)** — as-built은 [entity_resolution_layers.md](entity_resolution_layers.md)(2계층: 결정적 링킹 + LLM proposer/validator, 라운드마다 발동). 이 문서는 *구현 전 계획*으로 보존. · 짝 문서: [entity_resolution_gap.md](entity_resolution_gap.md)
> 목표: 발견된 mention/`Entity`를 **정규화 → 타입화 → 올바른 앵커에 타입 엣지 연결**해
> walk·프로파일이 1급 근거로 쓰게 한다. 우선 검증 케이스: BTS "Dynamite"의 ARMY·하이브.

> 2026-06-25 구현 방향 개정: `real_estate` Janitor 원칙을 따른다. 자동 승격은
> **강한 근거가 있는 high-precision 케이스만** 허용하고, 나머지는
> `SAME_AS_CANDIDATE` 또는 `Entity.needs_review=true` 후보로 남긴다. `guess_label`은
> 승격 근거가 아니라 약한 힌트다.

---

## 0. 핵심 아이디어 (한 문단)

지금은 `harvester._link_mentions`가 mention을 **기존 시드 노드와 이름 매칭**만 하고,
실패하면 `Entity{name, guess_label}`로 떨군 뒤 끝낸다(승격 없음). 여기에
**CONSOLIDATE(통합) 스테이지**를 EXPAND/담론 직후·COMPOSE 직전에 새로 끼워,
미해소 Entity를 ① 중복 병합 → ② 타입 노드로 승격 → ③ 올바른 앵커(Work/Artist)에 타입
엣지로 연결 → ④ 그 Entity를 가리키던 `Fact-[:MENTIONS]`를 승격 노드로 재연결, 한다.
그러면 ARMY가 `(BTS)-[:HAS_FANDOM]->(Fandom{ARMY})`가 되고 popularity walk가 근거로 쓴다.

---

## 1. 파이프라인 배치

```
현재: seed → expand(harvest→Fact+Entity) → [write ×축] → synth → finalize
계획: seed → expand → ★CONSOLIDATE★ → [write ×축] → synth → finalize
                         (담론 경로: outline 다음, write 직전)
```

- **순차** ([report.py](../../music_copyright/graphflow/report.py) `generate_report`): `run_expand(...)` 직후·축 루프 전에 `consolidate_entities(driver, work_id)` 호출.
- **LangGraph** ([workflow.py](../../music_copyright/graphflow/workflow.py)): 신규 노드 `consolidate` 추가.
  - 정적: `expand → consolidate → write_*`
  - 담론: `outline → consolidate → write_*` (리서처가 만든 Entity까지 포함되도록 outline 뒤)
- **멱등**: CONSOLIDATE는 MERGE 기반이라 재실행해도 중복 안 생김.
- **결정성 주의**: LLM 분류는 자동 승격에 쓰지 않는다. 후보 표시까지만 허용한다.

---

## 2. 온톨로지 확장 ([schema.py](../../music_copyright/graphflow/graph/schema.py))

### 신규 노드 라벨
| 라벨 | 키 | 의미 |
|---|---|---|
| `Fandom` | `fandom_id` | 팬덤(ARMY 등). **artist-scoped 키** `fandom:{artist_key}:{norm_name}` |
| `Agency` | `agency_id` | 소속사·매니지먼트(레이블과 분리). 공유 엔티티(글로벌 키) |

`Release`/`Genre`/`MediaEvent`/`ViralTrend` 같은 확장 라벨은 스키마에 둘 수 있지만,
자동 anchor edge는 만들지 않는다. 수상·차트는 기존 `Award`/`Chart` 재사용을 원칙으로
하되, 신규 생성은 review 이후로 미룬다.

### 신규 엣지 타입
| 엣지 | 방향 | 의미 |
|---|---|---|
| `HAS_FANDOM` | Artist → Fandom | 이 아티스트의 팬덤 |
| `MANAGED_BY` | Artist → Agency | 이 아티스트의 소속사 |

`WON`(Work/Artist→Award), `CHARTED_ON`(Work→Chart)은 승격 시 재사용.

### 부수 작업
- `_UNIQUE_KEYS`에 (Fandom, fandom_id)·(Agency, agency_id) 추가 → CONSTRAINTS/KEY_PROP 자동 반영.
- `MUSIC_SCHEMA` node_labels/edge_types 자동 포함(클래스 순회).

---

## 3. 신규 모듈 & 에이전트

- **모듈**: `music_copyright/graphflow/consolidate.py` (이름 주의: `resolver.py`는 ISWC
  인식자라 충돌 회피).
- **에이전트**(선택, 모호 케이스용): `music_entity_resolver` (gpt_mini) — `config.py`
  _DEFAULTS agents + `config/models.json` agents에 등록.
- **프롬프트 키**: `music_entity_resolver.system` ([prompts.py](../../music_copyright/graphflow/prompts.py) _DEFAULTS).

### 공개 API (예시 시그니처)
```python
def consolidate_entities(driver, work_id, *, mode="rules_first",
                         min_conf=0.6, use_llm=True) -> dict:
    """미해소 Entity → (병합) → 타입 승격 → 타입 엣지 → MENTIONS 재연결.
    반환 통계: {merged, promoted_by_label, edges_added, mentions_relinked}."""
```

---

## 4. CONSOLIDATE 알고리즘 (4단계)

### 입력 수집
- work 스코프(≤max_hops) 내 `Entity` 노드 + 각 Entity를 `MENTIONS`하는 Fact 텍스트(맥락)
  + `guess_label`. (스코프 한정 → 타 작품 누출 차단; `traversal.scope_eids` 재사용.)

### 4.1 정규화·중복 병합 (Step A)
- 정규화 이름(`normalize_text`) + 카드 임베딩 코사인(임계 τ≈0.9)으로 군집화.
- 예: "그래미"/"Grammy Awards"/"AMA(아메리칸 뮤직 어워드)" → 대표 1개로 병합
  (단, AMA≠Grammy는 분리 — 이름·맥락이 다르면 병합 금지. **과병합 위험**은 4.5 참조).
- 병합 = 대표 Entity로 MENTIONS 모으고 나머지 제거(또는 `SAME_AS` 표시).

### 4.2 타입 승격 (Step B) — high precision only
- **자동 승격**: `Fandom`처럼 이름/맥락이 강한 경우만. 예: `ARMY` → `Fandom`.
- **Agency 보강**: 신규 Entity에서 만들지 않고, 기존 `Rightsholder` 중 소속사로 판정 가능한
  노드만 `Artist-[:MANAGED_BY]->Agency`로 보강한다.
- **후보 처리**: `Award`/`Chart`/`Release`/`Genre`/`MediaEvent`/`ViralTrend`는 자동 생성하지
  않는다. 기존 스코프 노드와 정확히 맞으면 `SAME_AS_CANDIDATE`, 아니면
  `Entity.needs_review=true`, `candidate_label`로 남긴다.
- **LLM 폴백 금지**: `guess_label`과 LLM 판단은 후보 표시의 힌트일 뿐, 자동 승격·고가중
  anchor edge 생성 근거가 아니다.

### 4.3 타입 엣지 연결 (Step C) — 앵커 규칙
| 승격 타입 | 엣지 | 앵커 | 규칙 |
|---|---|---|---|
| Fandom | `HAS_FANDOM` | Artist | 팬덤명이 특정 아티스트 지칭 시 그 Artist; 모호하면 앵커 Work의 performer Artist |
| Agency | `MANAGED_BY` | Artist | performer Artist에 연결 |
| Award | `WON` | Work(우선)/Artist | 곡 맥락이면 Work, 아티스트 통산이면 Artist |
| Chart | `CHARTED_ON` | Work | |
| Artist | `BY_ARTIST`/`COVERED_BY` 등 | Work | 맥락 따라(보수적) |

- **중복 방지**: 이미 시드된 동일 Award/Chart 노드가 있으면 승격 대신 그 노드로 병합
  (예: 시드 Award `Q31155`와 Entity "MTV EMA").

### 4.4 MENTIONS 재연결 (Step D)
- 승격 전 `Fact-[:MENTIONS]->Entity`를 `Fact-[:MENTIONS]->승격노드`로 재연결(또는 추가)
  → **증거 체인 보존**(집필자가 인용 근거로 추적 가능). 원 Entity는 제거 또는 `superseded` 표기.
- 승격 노드 `card_text` 생성 + 임베딩(아래 6).

### 4.5 안전장치 (오염·과병합·멱등)
- **과병합 방지**: 임베딩 임계 + 이름 토큰 자카드 동시 충족 시에만 병합; LLM 병합 검증 옵션.
- **과승격 방지**: confidence 게이트 + 일반명사/메타진술 stoplist + drop 로그.
- **교차오염**: Fandom/Agency는 공유성 → `policies._HUB_LABELS`에 추가(absorbing leaf —
  evidence로 쓰되 통과 금지). Fandom 키는 artist-scoped라 타 아티스트로 안 샘.
- **멱등**: 전부 MERGE; 재실행 시 이미 승격된 건 skip(원 Entity 부재).

---

## 5. 프로파일 ([profiles.py](../../music_copyright/graphflow/profiles.py))

- `popularity`: `HAS_FANDOM` **0.9**(팬덤 동원력=대중성 핵심), `MANAGED_BY` 0.3.
- `influence`/`sustainability`: `HAS_FANDOM` 0.4~0.5(세대 팬덤=영향/지속 신호), `MANAGED_BY` 0.3~0.5.
- `social_value`: `HAS_FANDOM` 0.5(팬덤 주도 공익·기부 캠페인 연결 시), `MANAGED_BY` 0.2.
- `qa_base`: 두 엣지 0.5씩.
- 승격된 Award/Chart는 기존 `WON`/`CHARTED_ON` 고가중을 즉시 받음(추가 작업 불필요).

---

## 6. 카드·임베딩·인덱스

- [cards.py](../../music_copyright/graphflow/graph/cards.py): `card_for_fandom`(예: "ARMY —
  BTS의 팬덤. {활동 요약}"), `card_for_agency`("하이브 — BTS 소속사/매니지먼트.") + `_BUILDERS` 등록.
- [vector_index.py](../../music_copyright/graphflow/graph/vector_index.py) `INDEXED_LABELS`에
  `Fandom`/`Agency` 추가(SemanticPolicy가 축 의도로 부상).
- CONSOLIDATE 종료 시 승격 노드 임베딩 갱신(`update_embeddings_for_label` 또는
  curator 흐름 재사용).

---

## 7. 배선 변경 요약 (파일 체크리스트)

| 파일 | 변경 |
|---|---|
| `graph/schema.py` | Fandom/Agency 라벨, HAS_FANDOM/MANAGED_BY 엣지, _UNIQUE_KEYS |
| `graphflow/consolidate.py` (신규) | CONSOLIDATE 스테이지 + EntityResolver 에이전트 |
| `graphflow/prompts.py` + `config/music_prompts.json` | music_entity_resolver.system |
| `graphflow/profiles.py` | HAS_FANDOM/MANAGED_BY 가중치(축별) |
| `graph/cards.py` | card_for_fandom/agency + _BUILDERS |
| `graph/vector_index.py` | INDEXED_LABELS += Fandom/Agency |
| `graphflow/policies.py` | _HUB_LABELS += Fandom/Agency |
| `graphflow/report.py` | generate_report: run_expand 직후 consolidate 호출 |
| `graphflow/workflow.py` | consolidate 노드 + 엣지(expand→consolidate→write / outline→consolidate→write) |
| `graphflow/config.py` + `config/models.json` | music_entity_resolver=gpt_mini, consolidate 튜닝 |
| `scripts/test_e2e.py` | 승격 검증 단언 추가(아래 9) |

---

## 8. 엔드투엔드 신호 사슬 (왜 이게 ARMY를 살리나)

```
popularity navigator(팬덤 질의 조타, 이미 구현)
  → harvester: "BTS ARMY ..." 웹문서 → Fact "BTS는 ARMY를..." → Entity{ARMY}
  → ★CONSOLIDATE★: Entity{ARMY} --타입승격--> Fandom{ARMY}
                    (BTS:Artist)-[:HAS_FANDOM]->(Fandom{ARMY})
                    Fact-[:MENTIONS]->Fandom{ARMY} (재연결)
  → compose(popularity): HAS_FANDOM 0.9로 walk가 Fandom 도달 → evidence
  → writer(popularity): "팬덤 ARMY ..."를 [E#] 인용해 동원력 평가
```

---

## 9. 검증 계획

- **단위/구조(오프라인)**: schema 라벨·엣지·KEY_PROP, profiles 가중치, build_card,
  build_report_graph(consolidate 노드 포함) compile — `social_value` 때와 동일 스모크.
- **그래프 검증(Neo4j)**: CONSOLIDATE 후 `(BTS)-[:HAS_FANDOM]->(Fandom)` 존재,
  Entity 무덤 수 감소(285→줄어듦), Award/Chart 승격 수.
- **e2e**(`test_e2e.py` 단언 추가): Fandom 노드 ≥1 + Artist 연결, popularity claim이 Fandom
  근거 인용, ARMY가 Entity→Fandom으로 승격. before/after Entity 카운트 출력.
- **회귀**: social_value 이중 트랙·4축은 불변(consolidate는 증거 보강일 뿐 축 구조 무관).

---

## 10. 단계적 전달 (Phasing)

- **Phase 1 — 수직 슬라이스**: Fandom/Agency 타입 + 승격 규칙(guess_label 기반)만.
  ARMY·소속사 문제 직접 해결, 승격 메커니즘 검증. (LLM 폴백 없이 규칙만으로 시작 가능.)
- **Phase 2 — 일반화**: guess_label 전체 매핑 + LLM 폴백 + 중복 병합(Award/Chart 포함).
  Entity 무덤 285개 승격 → "다양한 엔티티 자동 구축·탐색" 비전 완성.
- **Phase 3 — 보강(선택)**: Fandom 메트릭(공식 팬클럽·해시태그·스밍 캠페인) 집계,
  Campaign/Collaboration 등 타입 추가, 승격 감사(LLM 모순 탐지).

---

## 11. 미결 설계 결정 (구현 전 확정 필요)

1. **Fandom 키 전략**: artist-scoped(`fandom:{artist}:{name}`) 확정? — 같은 팬덤이
   여러 곡 평가에서 공유되되 타 아티스트로 안 새게.
2. **Agency vs Rightsholder**: Agency를 새 라벨로 분리 vs 기존 Rightsholder에 `kind=소속사`
   세분 + Artist 연결 추가. (분리가 깔끔하나 시드 Rightsholder와의 관계 정의 필요.)
3. **Entity 처리**: 승격 후 원 Entity **삭제** vs `superseded_by` 보존(감사 추적용).
4. **결정성**: 정적 보고서 경로에서 LLM 분류 허용 여부(rules_only 모드 기본값?).
5. **승격 신뢰 임계**(min_conf)와 stoplist 범위.
