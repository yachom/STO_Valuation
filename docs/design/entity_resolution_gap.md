# 엔티티 해소·승격 계층 부재 (Entity Resolution Gap)

> 진단일: 2026-06-25 · 대상: `music_copyright` graphflow · 브랜치: `yachom/feature/music-copyright`
> 검증 케이스: BTS "Dynamite" (`work_id=T-300.516.442-7`)
>
> **✅ 상태: 해소됨(2026-07-01).** 이 문서는 *갭 진단*(구현 전)이다. 여기서 지적한 Entity 무덤·승격
> 계층 부재는 이후 구현됐다 — 구현 전 계획은 [`entity_resolution_plan.md`](entity_resolution_plan.md),
> **as-built(현행 2계층)** 은 [`entity_resolution_layers.md`](entity_resolution_layers.md).
> (본문의 코드 라인번호 예: `harvester.py:314`는 *진단 시점* 기준으로 이후 이동함 — 현행 위치는
> layers 문서 부록 참조. 이 문서는 역사적 진단으로 보존.)

## TL;DR

음악 평가 그래프는 **웹에서 엔티티를 *발견*하지만, 그것을 타입 노드로 *구축*하지 않는다.**
하베스터가 찾아낸 ARMY·Grammy·Billboard Hot100·AMA 등 가치 있는 엔티티들이 전부
제네릭 `Entity` 노드("미해소 추출물") 무덤(스코프 내 132개, DB 전체 285개)에 방치돼,
평가 walk가 근거로 쓰지 못한다. 결과적으로 **"대중성·팬덤" 축이 BTS의 팬덤(ARMY)을
바로 옆 노드로 두고도 평가하지 못한다.** 시스템이 *엔티티 중심 지식그래프 구축*이 아니라
*사실 중심 RAG*에 머물러 있기 때문이다.

---

## 1. 증상

평가가 마땅히 다뤄야 할 엔티티가 그래프에 1급 노드로 안 보인다:

- **팬덤(ARMY)** — "대중성·팬덤" 축의 핵심인데 평가에 안 잡힘.
- **소속사(하이브/BigHit)** — 아티스트와 직접 연결돼야 하는데 그렇게 안 돼 있음.

집필자도 이 공백을 정직하게 적었다 (popularity 섹션 claim):
> "공식 팬클럽 활동량, 소셜미디어 해시태그, 챌린지 참여, 구매 동원 같은 팬덤 조직성
> 지표가 없어 팬덤의 동원력은 직접 평가하기 어렵다."

ARMY 노드가 스코프 안에 **있는데도** 못 쓴 것이다.

---

## 2. 증거 (실제 그래프 조회)

### 2.1 ARMY는 "미해소 Entity"로만 존재
```
(Fact: "BTS는 ARMY를 보호하기 위해 가사를 해석하는 콘텐츠를 제공했다")
        -[:MENTIONS]-> (Entity {name:"ARMY", guess_label:null})
```
- 타입은 `Fandom`이 아니라 제네릭 `Entity`.
- 연결 엣지는 `Fact-[:MENTIONS]->Entity` **딱 하나**. BTS Artist 노드와 직접 엣지 없음.
- Dynamite 스코프(≤4홉) **내에 존재**(True) — 그런데도 평가에 투명인간.

### 2.2 Entity 무덤 — 발견했으나 버려진 엔티티들
스코프 내 `Entity` 132개 / DB 전체 285개. guess_label만 붙은 채 승격 안 됨:
```
American Music Awards(추정:Award)   Grammy Awards(추정:Award)
빌보드 메인 싱글차트 '핫100'(추정:Chart)   AMA(아메리칸 뮤직 어워드)
기네스 북(추정:Award)   올해의 아티스트(추정:Award)   ARMY ...
```
"Grammy Awards" Entity는 `Award` 노드로 합쳐지지 않고, "ARMY"는
`(BTS)-[:HAS_FANDOM]->(Fandom)`이 되지 않는다.

### 2.3 소속사는 잡혔으나 잘못 모델링됨
```
(Work)-[:HELD_BY]->(Rightsholder {name:"하이브", kind:"레이블"})
(Work)-[:HELD_BY]->(Rightsholder {name:"BigHit Entertainment", kind:"레이블"})
```
- **노래의 권리자**로 모델링 — 음반사/유통사/소속사·매니지먼트를 "레이블"로 뭉뚱그림.
- **Work에 붙음, Artist(BTS)에 안 붙음.** 그래서 "방탄소년단—소속사" 엣지가 없다.

### 2.4 (참고) 교차오염 격리는 정상
DB에 김호중/아리스 팬덤 Fact가 있지만 Dynamite 스코프 누출 **0건** — 작품-앵커링·
hub-as-leaf 격리는 잘 작동 중. (이 문제와 별개.)

---

## 3. 데이터 흐름 분석 — Entity는 어디서 생기고 왜 죽나

### 생성: `harvester._link_mentions` ([harvester.py:314](../../music_copyright/graphflow/harvester.py))
하베스터가 웹 문서에서 Fact를 추출할 때, Fact가 언급한 대상(mention.surface)을
**기존 스코프 노드 이름과 매칭**한다:
- 매칭 성공 → `Fact-[:MENTIONS]->(기존 타입 노드)` (확정 연결)
- 매칭 실패 → `Entity{name, guess_label}` 신규 + `Fact-[:MENTIONS]->Entity`

```python
if matched:                          # 기존 스코프 노드와 이름 매칭
    _merge_edge(s, L.FACT, fact_id, E.MENTIONS, label, key)
else:                                # 미해소 → Entity (여기서 끝)
    eid = short_hash(nm)
    _merge_node(s, L.ENTITY, eid, {"name": m.surface, "guess_label": m.guess_label, ...})
    _merge_edge(s, L.FACT, fact_id, E.MENTIONS, L.ENTITY, eid)
```

### 사망: 승격 단계가 없음
- `guess_label="Award"`인 Entity를 실제 `Award` 노드로 바꾸는 코드가 **없다.**
- 중복 Entity(그래미/Grammy/AMA)를 병합하는 코드가 **없다.**
- Entity를 올바른 앵커(Work/Artist)에 타입 엣지로 잇는 코드가 **없다.**
- 스키마에 `SAME_AS_CANDIDATE` 엣지 타입은 정의돼 있으나 실제로는 생성조차 안 됨
  (하베스터는 `MENTIONS`만 만든다).

### 핵심 결함: 해소가 "기존 노드 매칭"에만 의존
mention은 **이미 시드된 노드와만** 매칭된다. `Fandom`은 시드 소스가 없어(아래 4절)
애초에 시드 안 되므로, "ARMY"는 매칭할 대상이 없어 **항상** Entity로 떨어진다.

---

## 4. 근본 원인

### ① 닫힌 온톨로지 (Closed Schema)
노드 라벨이 고정 집합이다 ([schema.py](../../music_copyright/graphflow/graph/schema.py)):
`Work/Artist/Rightsholder/Platform/Chart/Award/Certification/SocialMetric/
SocialTheme/Cause/PublicUse` + 웹(`WebDoc/Fact/Entity/Query`) + 보고서.
**`Fandom`도, 독립적 `Agency`도 없다.** 정형 시드는 4개 API(Wikidata·MusicBrainz·
Genius·Soundcharts)가 *반환하는 타입*만 만들 수 있는데, 그 어느 것도 "팬덤"을 주지 않는다.

### ② 엔티티 해소·승격 계층 부재 (핵심)
파이프라인이 **사실 중심 RAG**다: 웹 → 원자적 Fact + 미해소 Entity → 의미검색.
**엔티티 중심 KG 구축**(발견된 mention을 정규화·타입화·연결해 1급 엔티티로 승격)
계층이 없다. 발견(harvest)은 되는데 구축(typing·resolution·linking)이 안 된다.

### ③ 소속사 모델 혼동
Agency(소속사/매니지먼트)와 Label(음반사)·Publisher를 전부 `Rightsholder`로 뭉치고,
Work에만 연결한다. 아티스트의 소속·관리 관계가 표현되지 않는다.

---

## 5. 영향 범위

- **대중성·팬덤 축**: 팬덤 조직력·동원력을 평가 불가(ARMY가 Entity 무덤에).
- **영향력 축**: Grammy·AMA·Billboard 등 수상/차트가 Entity로만 존재하면 walk가
  WON/CHARTED_ON 고가중 경로로 못 밟아 근거에서 누락(시드로 잡힌 일부만 사용).
- **전 축 공통**: 285개 Entity = 발견된 가치 신호의 대량 손실. "다양한 엔티티 자동
  구축·탐색"이라는 그래프 DB 채택 목적 자체가 절반만 달성됨.

---

## 6. 수정 설계 — 엔티티 해소·승격 계층

> **상세 구현 계획은 [entity_resolution_plan.md](entity_resolution_plan.md) 참조.** (이 절은 설계 요약.)

목표: 발견된 mention/Entity를 **정규화 → 타입화 → 올바른 앵커에 타입 엣지 연결**해,
walk·프로파일이 1급 근거로 쓰게 한다.

### 6.1 온톨로지 확장 (schema)
- 노드: `Fandom`(팬덤), `Agency`(소속사·매니지먼트, 레이블과 분리). 필요시
  `Campaign`/`Collaboration` 등.
- 엣지: `HAS_FANDOM`(Artist→Fandom), `MANAGED_BY`(Artist→Agency).
  (수상·차트는 기존 `WON`/`CHARTED_ON` 재사용해 Work/Artist에 연결.)

### 6.2 해소·승격 단계 (harvester/expand 직후 신규 스테이지)
미해소 Entity + 이를 언급한 Fact를 입력으로:
1. **정규화·중복 병합**: "그래미"/"Grammy Awards"/"AMA(아메리칸 뮤직 어워드)" → 1개.
2. **타입 승격**: `guess_label` + 언급 Fact 맥락으로 Entity → 타입 노드
   (Entity→Award/Chart/Fandom/Agency …).
3. **타입 엣지 연결**: Fandom→Artist(HAS_FANDOM), Award→Work(WON),
   Chart→Work(CHARTED_ON), Agency→Artist(MANAGED_BY).
4. 결정/감사: 규칙(guess_label 신뢰 시 결정적) + 모호 시 LLM 판정. 멱등(MERGE).

### 6.3 프로파일 (popularity 등)
- `HAS_FANDOM` 고가중(popularity) → ARMY가 팬덤 동원력 근거로 부상.
- 승격된 Award/Chart는 기존 고가중(WON/CHARTED_ON)을 그대로 받아 즉시 walk 가능.

### 6.4 Fandom 신호 보강(선택)
popularity navigator/harvester(이미 팬덤 질의로 조타됨)가 모은 Fact를 Entity 무덤
대신 **타입 Fandom 노드**에 귀속 → 공식 팬클럽·해시태그·스밍 캠페인 등이 평가 가능해짐.

---

## 7. 스코프 옵션 (구현 진행 시)

- **①(작게)**: `Fandom`/`Agency` 타입 + 승격 규칙만 — ARMY·소속사 문제 직접 해결.
- **②(크게)**: Entity 무덤 전체 승격(Award/Chart/Fandom/Agency…) + 중복 병합 — "다양한
  엔티티 자동 구축·탐색" 비전 완성.

---

## 부록 — 관련 코드 위치

| 관심사 | 파일 |
|---|---|
| mention 해소 / Entity 생성 | `music_copyright/graphflow/harvester.py:314` `_link_mentions` |
| 노드 라벨/엣지 온톨로지 | `music_copyright/graphflow/graph/schema.py` |
| 축별 엣지 가중치(walk 우선순위) | `music_copyright/graphflow/profiles.py` |
| 정형 시드(어떤 타입을 만드나) | `music_copyright/graphflow/seeder.py` `_collect_*` |
| 카드(임베딩 텍스트) | `music_copyright/graphflow/graph/cards.py` |
| 허브-as-leaf 격리(교차오염) | `music_copyright/graphflow/policies.py` `_HUB_LABELS` |
