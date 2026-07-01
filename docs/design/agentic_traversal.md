# 에이전트형 그래프 탐색 — 적응적 깊이 + PPR recall (설계)

> 작성일: 2026-06-30 · 대상: `music_copyright/graphflow` (이후 `infra`로 승격해 `real_estate` 공유)
> 배경: 현재 그래프 탐색은 **고정 멀티홉**(`retrieval.max_hops`)과 **손튜닝 엣지 가중치**
> (`profiles.py`)에 의존한다. 둘 다 무논문 ad-hoc 산물이다. 이를 GraphRAG 문헌
> (HippoRAG 2 · Adaptive-RAG · ToG/PoG)에 근거해 재정초한다.
> 관련: [`graph_native_flow.md`](graph_native_flow.md) §5(walk), [`entity_resolution_layers.md`](entity_resolution_layers.md)
> 메모리: `music-graphrag-paper-upgrade`

> **⚠️ 구현 현황(2026-07-01 갱신) — 문서와 실제의 차이(먼저 읽을 것)**
> - **Tier 2 compose-recall PPR(`ppr.ppr_recall`, §5)**: **구현·배선됨**, config `retrieval.recall.mode` 플래그 뒤(기본 `walk`=불변). `edge_prior`(profile|uniform|semantic)·adaptive 깊이(§4)도 config에 존재(기본 off). → 설계대로.
> - **★ PPR을 "탐색"보다 "수집 frontier"에 먼저 적용(신규·항상 켜짐)**: `ppr.ppr_frontier_scores` → `navigator.collect_frontier`. 즉 이 문서가 다루는 *compose 깊이*보다 **확장 대상 선택**에 PPR을 먼저 넣었다.
> - **§1.1의 "적재 폭(웹수집 라운드) = 이미 적응적"은 정정**: 실제 frontier 점수는 상수 `_LABEL_PRIORITY`, 정지는 `min_query_utility` 임계라 **관계 기반이 아니었다**(사용자 지적: "상수 곱·임계는 폐기 대상"). → Work 앵커 **구조 PPR** frontier + **facts_new/entities_new** 기반 정지로 교체. 같은 PPR 정신(홉 상수 제거·구조 전파)을 수집단에 실현.
> - 상세: 메모리 `social-value-redesign`(관계 기반 프론티어 항).

---

## 0. 한 문단 요약

평가대상(Work) 앵커에서 그래프를 **얼마나 멀리·어느 관계로** 탐색할지를, 지금은 코드 밖
상수(`max_hops`)와 손튜닝 표(`profiles.py`)가 정한다. 이 문서는 그 결정을 **데이터·의도
기반**으로 바꾼다: ① 축별로 탐색 깊이를 적응적으로 라우팅(**Adaptive-RAG**), ② 고정홉 walk
recall을 **Personalized PageRank(HippoRAG 2)**로 대체해 "홉 상수" 자체를 없앤다(감쇠가 깊이를
제어). 손튜닝 가중치(`profiles.py`)는 버리지 않고 **PPR 전이확률의 prior로 재사용**한다. 모든
신규 동작은 플래그 뒤에 격리하며 기본 경로(`recall.mode=walk`)는 불변이다. 더 멀리 가는
에이전트형 빔 탐색(ToG/PoG)과 학습형 retriever(GFM-RAG)는 후속 Tier로 둔다.

---

## 1. 문제 — 현재의 고정 멀티홉

### 1.1 두 개의 멀티홉 노브
파이프라인에는 멀티홉을 통제하는 두 지점이 있다:

| 노브 | 위치 | 현재 상태 |
|---|---|---|
| **적재 폭** (웹수집 라운드) | `expand.run_expand` (`expand.py:158`) | **이미 적응적** — novelty 임계·`max_poor_rounds`·예산으로 동적 정지 |
| **탐색 깊이** (보고서-타임 walk) | `compose` → `walk(max_hops=…)` (`composer.py:211`) | **완전 고정** — `config retrieval.max_hops`(현재 5) |

이 문서가 고치는 것은 **두 번째**다(적재 폭은 이미 적응적이므로 §9에서 정합만 맞춘다).

### 1.2 고정 `max_hops`의 한계
`compose`는 모든 축·모든 작품에 동일한 `max_hops`로 walk한다(`composer.py:186, 211`).
- **축마다 최적 깊이가 다르다.** `popularity`는 Work 1–2홉(차트·지표)에 답이 있고,
  `social_value`는 가사→의제→운동→공익활용으로 더 깊이 가야 신호가 나온다. 단일 상수는 한쪽을
  과잉탐색하거나 다른 쪽을 과소탐색한다.
- **작품마다 그래프 밀도가 다르다.** 잘 알려진 곡은 2홉에 증거가 포화되고, 신인곡은 더 들어가야
  한다. 고정홉은 이를 못 본다.
- **곱셈 깊이 페널티의 절벽.** walk 점수는 `∏ edge_weight × node_score`로 단조감소하고
  (`walk.py:218`), `max_hops`에서 **하드 컷**된다. 컷 직전의 약한-그러나-관련 노드와 컷 직후의
  노드 사이에 의미적 근거 없는 경계가 생긴다.

### 1.3 `profiles.py` 손튜닝 가중치의 한계
축별 엣지 가중치(`profiles.py`)는 코드 주석부터 `⚠️ TODO(domain): tier 배치는 placeholder
가설`이다. 절대값이 손으로 박혀 있고(예: social_value의 `CHARTED_ON=0.2`), 골든 작품
눈검사로만 정련하게 되어 있다.
- **웹전용 모드에서 더 안 맞는다.** 정형 API(soundcharts/lastfm)를 끄면 상업 노드의 밀도·신뢰도
  우위가 사라진다. 손튜닝 억제값(0.2)은 "상업 노드가 조밀·고신뢰"라는 전제 위에 보정된
  값이라(→ [profiles 억제의 6가지 이유, 본 설계 §부록 참조 또는 회의록]) 전제가 바뀌면 재튜닝이
  필요하다. 손튜닝은 확장 불가능한 유지보수 부채다.

> **핵심 통찰**: §1.2와 §1.3은 같은 문제다 — *탐색 결정이 코드 밖 상수에 박혀 있다*. PPR은
> 깊이 상수를 없애고, 손튜닝 표를 의미 있는 전이 prior로 강등한다(아래 §5).

---

## 2. 논문 근거 (매핑)

| 논문 | 가져오는 것 | 본 프로젝트 코드 매핑 | 이번 Tier |
|---|---|---|---|
| **HippoRAG 2** (ICML'25, [2502.14802]) | 임베딩이 passage+triple을 채점해 **seed 노드** 선정 → **Personalized PageRank**로 멀티홉 1회 회수 | seed=`traversal.semantic_sims`(축의도 코사인), triple=`Fact`, passage=`WebDoc`, 임베딩=e5 1024d(`infra.graph.embedder`) — **구성요소 이미 전부 보유** | **Tier 2** |
| **Adaptive-RAG** (NAACL'24, [2403.14403]) | 질의 복잡도 분류 → {무검색 / 단일홉 / 멀티홉} 라우팅 | 축+그래프 밀도(`coverage.axis_coverage`)로 축별 깊이 라우팅 | **Tier 1** |
| **Plan-on-Graph / PoG** (NeurIPS'24, [2410.23875]) | 적응적 *폭* + Memory + **Reflection(자기수정·동적 정지)** | 반성 기반 정지(`post_write_gap_feedback` 재사용·확장) | Tier 1(반성) / Tier 3(빔) |
| **Think-on-Graph / ToG·ToG-2** ([2307.07697]/[2407.10805]) | LLM 에이전트 **빔 탐색** — 매 홉 관계·이웃을 LLM이 채점·프루닝 | gap-flagged 축에 한해 에이전트 traversal (`WalkPolicy` seam) | Tier 3 |
| **GFM-RAG / G-Reasoner** ([2502.01113]/[2509.24276]) | 학습된 GNN retriever, 멀티홉 단일 forward, 자산 간 전이 | infra 공통 retriever(음악+부동산 하나의 모델) | Tier 4 |

---

## 3. 목표 아키텍처 — 성숙도 사다리

| Tier | 내용 | 깊이 결정 주체 | 비용 | 상태 |
|---|---|---|---|---|
| **0** (현재) | 고정 `max_hops` walk + 손튜닝 `profiles.py` | config 상수 | 최저 | 가동 중 |
| **1** | 축별 적응 깊이 라우팅 + 반성 정지 | 휴리스틱/LLM 라우터 | 저 | **이번 구현** |
| **2** | HippoRAG2 PPR recall (홉 상수 제거) | PPR 감쇠(restart_prob) | 저(결정적) | **이번 구현** |
| **3** | ToG/PoG 에이전트 빔 탐색 (gap-gated) | LLM 매 홉 판단 | 고(LLM 다회) | 로드맵 |
| **4** | GFM-RAG/G-Reasoner 학습 retriever | 학습된 GNN | 학습 1회+추론 저 | 연구지평 |

**이번 범위 = Tier 1 + Tier 2.** 둘은 직교한다(라우팅과 recall 엔진). 합치면: *라우터가 축별로
깊이/recall 모드를 고르고, PPR이 그 깊이를 감쇠로 실현*한다.

---

## 4. Tier 1 — 적응적 깊이 + 반성 정지

### 4.1 Adaptive-RAG 라우터 (coverage → depth)
축마다 탐색 깊이를 다르게 준다. 입력은 **그래프 밀도**(`coverage.axis_coverage`가 이미 축별
`n_facts`/`n_press`/`n_queries`를 센다)와 **축 종류**.

```
# music_copyright/graphflow/routing.py (신규)
ROUTE = {"shallow": 2, "standard": 4, "deep": 7}   # config.retrieval.adaptive.depths

def route_axis_depth(coverage_for_axis: dict, axis: str, cfg: dict) -> int:
    """축별 recall 깊이(=PPR scope_hops 또는 walk max_hops) 결정. 휴리스틱 우선."""
    if not cfg["retrieval"]["adaptive"]["enabled"]:
        return cfg["retrieval"]["max_hops"]            # Tier0 동작(불변)
    n_facts = coverage_for_axis.get("n_facts", 0)
    # 밀도 높음 → 얕게(이미 포화), 낮음 → 깊게(더 들어가야 신호)
    if n_facts >= cfg["retrieval"]["adaptive"]["dense_facts"]:   # e.g. 12
        return ROUTE["shallow"]
    if n_facts <= cfg["retrieval"]["adaptive"]["sparse_facts"]:  # e.g. 3
        return ROUTE["deep"]
    return ROUTE["standard"]
```

- **휴리스틱 먼저(LLM 0회).** Adaptive-RAG의 분류기는 추후 `music_axis_router` 에이전트로 교체
  가능하게 `router: heuristic|llm` 플래그를 둔다. 초기엔 휴리스틱으로 충분(밀도가 곧 복잡도
  프록시).
- **사회축 바닥값.** `social_value`는 밀도 무관 최소 `standard`(상업 frontier가 사회 신호를
  굶기는 기존 문제와 동형 — `navigator._ensure_social_floor`의 retrieval-time 판본).

### 4.2 반성 정지 (PoG Reflection 재사용)
이미 `post_write_gap_feedback`(`report.py:214`)가 집필 후 "근거 부족" 클레임·정량 빈약 축을
잡아 재확장한다 — 이것이 PoG의 Reflection의 *집필-타임* 절반이다. Tier 1은 이를 **recall-타임**
으로 당긴다:

- `compose` 직후 회수된 후보 수가 `min_evidence` 미만이면 그 축의 깊이를 1단계 승급
  (`shallow→standard→deep`)해 1회 재회수. 상한은 `deep`. (집필까지 가서 gap_feedback이
  돌기 전에 싸게 끊는다.)

### 4.3 config 스키마 + 삽입 지점
```jsonc
// config/music_graphflow.json — retrieval 블록에 추가 (기본 비활성 → Tier0 불변)
"retrieval": {
  "evidence_k": 28, "max_hops": 5, /* ...기존... */
  "adaptive": {
    "enabled": false,             // ← 켜야 동작
    "router": "heuristic",        // heuristic | llm
    "depths":  {"shallow": 2, "standard": 4, "deep": 7},
    "dense_facts": 12, "sparse_facts": 3,
    "social_floor": "standard"
  }
}
```
- **삽입 지점**: `report.write_axis`(`report.py:64`)가 `compose`를 부르기 직전에
  `route_axis_depth(...)`로 `max_hops`를 계산해 `compose(..., max_hops=routed)`로 넘긴다.
  `compose`는 이미 `max_hops` 인자를 받는다(`composer.py:164`) — **시그니처 변경 없음**.
- **coverage 입력**: `coverage.axis_coverage`는 이미 존재(`coverage.py:53`). write 전에 1회 계산해
  축별로 나눠 라우터에 전달.

---

## 5. Tier 2 — HippoRAG 2 PPR recall 백본

### 5.1 왜 PPR이 고정홉을 제거하나
walk는 `max_hops`에서 경로를 하드 컷한다. **Personalized PageRank**는 컷이 없다: restart
확률 `(1-α)`로 시드(=앵커+축의도 노드)로 계속 되돌아오며 그래프 전역에 점수를 퍼뜨린다. 먼
노드는 도달하되 **거리만큼 지수 감쇠**한다. 즉:

> 깊이 = `restart_prob` + 엣지 전이확률이 만드는 **창발적(emergent) 양**이지, config 정수가
> 아니다. 이것이 §1.2 "홉 상수 절벽"의 근본 해소다.

그리고 이것은 정확히 HippoRAG 2다: *"임베딩 모델이 passage와 triple을 채점해 PPR의 seed
노드를 식별"*. 본 프로젝트는 그 부품을 이미 다 가졌다:

| HippoRAG 2 부품 | 본 프로젝트 보유물 |
|---|---|
| open KG triple | `Fact`(+`MENTIONS`→typed node) |
| passage | `WebDoc` |
| 임베딩 seed 채점 | `traversal.semantic_sims`(축의도 코사인, eid→[0,1]) |
| PPR | **신규**(순수 파이썬, §5.3) |

### 5.2 scope 서브그래프 + 시드 + 전이 prior (`profiles.py` 재사용)
PPR은 작은 그래프 위에서 돈다. 본 프로젝트는 **이미 hub-aware scope**를 가진다
(`traversal.scope_eids`, `traversal.py:67`) — 공유 허브 통과 경로를 쳐내 타 작품 누출을 막는
바로 그 서브그래프다. 이 위에서:

1. **노드 집합** `V = scope_eids(driver, work_id, scope_hops)` (PPR은 감쇠가 깊이를 제어하므로
   `scope_hops`를 walk보다 넉넉히, 예 6).
2. **간선·전이확률** `M`: scope 내 간선을 1회 Cypher로 당겨, 각 간선 가중치 =
   `profiles.PROFILES[axis][rel_type]` (없으면 `DEFAULT_WEIGHT`). **여기서 손튜닝 표가 prior로
   재사용된다** — walk의 점수 배수가 아니라 *전이확률의 비대칭*으로. (social_value의
   `CHARTED_ON=0.2`는 "Chart로 잘 안 흘러가는 전이"가 되어 의미가 동일하게 보존되나, 절대 컷이
   아니라 확률 질량의 재분배가 된다.)
3. **시드(personalization) 벡터** `s`: `semantic_sims(driver, work_id, axis_intent, scope_hops)`로
   축의도와 가까운 노드에 질량 집중 + 앵커에 고정 질량. 정규화해 합=1.

### 5.3 순수 파이썬 PPR (GDS/APOC 없음)
`docker-compose.yml`은 Neo4j **Community 5.26**(GDS·APOC 미설치)다. scope가 작으므로
in-memory power iteration으로 충분하다.

```
# music_copyright/graphflow/ppr.py (신규) — 또는 infra.graph.ppr (공통 승격, §7)
def personalized_pagerank(adj, seed, *, restart=0.15, max_iter=30, tol=1e-6):
    """adj: {eid: [(nbr_eid, weight), ...]} (out-정규화 전), seed: {eid: mass(합=1)}.
    반환: {eid: ppr_score}. 결정적(반복·정렬 고정)."""
    nodes = list(adj.keys())
    # 행정규화된 전이(가중치 합으로 나눔), dangling은 seed로 복귀
    out = {u: (sum(w for _, w in nbrs) or 0.0) for u, nbrs in adj.items()}
    p = dict(seed)                                   # 초기값 = personalization
    for _ in range(max_iter):
        nxt = {u: restart * seed.get(u, 0.0) for u in nodes}
        for u in nodes:
            mass = (1 - restart) * p.get(u, 0.0)
            if out[u] <= 0:                          # dangling → 시드로
                for v in nodes:
                    nxt[v] += mass * seed.get(v, 0.0)
                continue
            for v, w in adj[u]:
                nxt[v] += mass * (w / out[u])
        if sum(abs(nxt[u] - p.get(u, 0.0)) for u in nodes) < tol:
            p = nxt; break
        p = nxt
    return p
```
- **결정성**: 보고서 재현성에 중요. PPR은 결정적이다(현재 recall의 LLM rerank만 비결정적).
- **비용**: scope 노드 수 N, 간선 E, 반복 I → `O(I·E)`. 자산 스코프는 보통 N<몇백 → 무시 가능.
  LLM 호출 0회.

### 5.4 `compose` 삽입 지점 (`recall.mode = walk|ppr|hybrid`)
현재 recall은 `composer.py:210-229`다(walk + `_scope_fact_candidates`). 여기에 분기를 둔다:

```
# composer.compose — recall 단계 (line ~210) 교체/분기
mode = r.get("recall", {}).get("mode", "walk")     # 기본 walk = Tier0 불변
if mode in ("ppr", "hybrid"):
    from music_copyright.graphflow.ppr import ppr_recall
    ppr_paths = ppr_recall(driver, work_id, axis, intent=news_intent or intent,
                           profile=profile, k=k * 3, cfg=r["recall"]["ppr"])
    paths = ppr_paths if mode == "ppr" else _dedup_paths(walk_paths + ppr_paths)
else:
    paths = walk(driver, [anchor], policy, max_hops=max_hops, k=k * 3)  # 기존
```
- `ppr_recall`은 §5.2–5.3을 묶어 `EvidencePath` 리스트를 반환(앵커→노드, 표시 경로는
  `_scope_fact_candidates`처럼 "PPR" 라벨). 이후 **구조 안전 필터·rerank·_enrich_origins는
  그대로**(`composer.py:215-238`) — precision 단계 불변.
- **hybrid**가 권장 시작점: walk(국소·경로해석)와 PPR(전역·다홉)을 합쳐 rerank가 고르게 한다.
  순수 ppr는 walk의 "경로 텍스트(`path.text()`)" 해석성을 잃으므로 평가 후 결정.

```jsonc
// config/music_graphflow.json — retrieval 블록
"recall": {
  "mode": "walk",                     // walk(legacy) | ppr | hybrid
  "ppr": { "scope_hops": 6, "restart_prob": 0.15, "max_iter": 30, "tol": 1e-6,
           "seed": "semantic", "edge_prior": "profile" }  // seed=semantic_sims+anchor; edge_prior: profile|uniform|semantic (§5.6)
}
```

### 5.5 `max_hops`의 의미 재정의
Tier 2 이후 `retrieval.max_hops`는 **두 의미로 분기**한다(혼동 방지 위해 문서화):
- `recall.mode=walk` → 기존 의미(walk 하드 컷).
- `recall.mode=ppr|hybrid` → walk 분기에만 적용; PPR 깊이는 `recall.ppr.scope_hops`(서브그래프
  반경)와 `restart_prob`(감쇠)가 정한다. Tier 1 라우터는 ppr 모드에서 `scope_hops`를 조정한다.

### 5.6 상수 병목 제거 — `edge_prior` 노브 (손튜닝 표 → 계산값)

**문제 재정의(핵심 지적)**: 진짜 병목은 `max_hops` 하나가 아니라 **누적 곱에 들어가는 엣지 상수
표**(`profiles.PROFILES`)다 — `O(축 × 엣지타입) ≈ 80개`를 사전에 손으로 박아야 한다. (코드 경로:
`profiles.PROFILES` → `StaticPolicy.edge_weight`(`policies.py:67`) → `walk`의 `∏ edge_weight`.)

PPR은 변별 신호를 *손튜닝 상수*에서 *데이터*로 옮긴다. **시드(personalization)는 항상 의미 기반**
(`traversal.semantic_sims`)이라 축 적합성은 edge_prior와 무관하게 이미 상수 없이 들어온다. 남은
전이확률의 엣지 prior에 노브를 둔다:

| `edge_prior` | 전이확률 | 손튜닝 상수 |
|---|---|---|
| `profile` | ∝ `profiles` 상수 | **잔존**(하드컷→소프트 prior로 강등) — back-compat 기본 |
| `uniform` | 모든 엣지 동일 | **제거** — 변별=의미시드+토폴로지+node 의미항(hub은 `scope_eids` pruning이 억제) |
| `semantic` | `cos(embed(엣지타입 설명), embed(축 의도))` 정규화 | **계산으로 대체** |

**`semantic`이 핵심.** `profiles`의 80개 숫자는 결국 "이 엣지가 이 축에 얼마나 관련 있나"의
손-근사다 — 그건 **임베딩으로 계산 가능**하다. 엣지타입 설명(이미 `schema.py`에:
`ADDRESSES`=`Work→SocialTheme (다루는 사회적 의제)`)을 축 의도(이미 `axes.py`에)와 코사인:
social_value 의도 vs `ADDRESSES`=고 / vs `CHARTED_ON`=저 → **§1의 손튜닝 억제(0.2)를 임베딩이
자동 재현**. 표를 안 짠다. 엣지타입 ~20 × 축 → 1회 precompute, 결정적.

> 정직한 단서: `semantic`도 엣지타입 *설명문*과 축 의도문은 인간 작성이다. 그러나 **축×엣지
> 숫자(80) → 0**, 새 엣지·새 축이 추가돼도 자동 계산(동적 축 `--axis`이 가중치 표 없이 도는 것과
> 동일 원리, `composer.py:178`). 유일 잔존 상수는 `restart_prob` **1개**.

**패러다임 전환**: walk = *경로 점수*(노드값 = 최선 단일경로의 상수 곱) → PPR = *개인화 중심성*
(노드값 = 의미 시드에서 재시작하는 확산 질량). 상수 *곱* → *확산*, 상수 *입력* → *임베딩*.

**완전 제거 로드맵**: 지금(Tier 2) `edge_prior=semantic`로 표 없는 검색 → Tier 3(ToG/PoG) 매 홉
LLM이 `(평가대상, 관계, 후보노드)`를 *사례별 판단* → 표·계산식 없이 **관계 의미로 확장**("노드·엣지
관계에 따라"의 완성형) → Tier 4(GFM-RAG) 가중치 **학습**.

### 5.7 임베딩 OOD vs LLM 비용 — 캐스케이드 (양자택일 아님)

**우려 ① 임베딩 OOD**: e5(frozen)는 미학습 사건·신조어에 정확도가 떨어진다 — 맞다. 단 위치별
위험이 다르고, 시스템이 **검색-grounded**라 생각보다 완화된다:
- `edge_prior=semantic`: **저위험** — 엣지타입 의미 vs 축 의미(둘 다 일반어), 특정 사건 지식 불필요.
- `semantic_sims`(노드 시드): 중위험 — 사건 *지식*이 아니라 *카드 텍스트의 토픽 유사도*를 본다.
  신규 사건도 카드에 단어(예: 챌린지/틱톡/확산)가 있으면 축 의도와 코사인이 잡힌다. 진짜 약점은
  *사건 OOD가 아니라* **설명 없는 신조어·terse 카드**.
- **3중 안전망(이미 보유)**: ① PPR은 토폴로지·corroboration도 쓴다(임베딩 only 아님, OOD 무관)
  ② recall은 generous(k×3) — *드롭만 안 하면* 됨 ③ **LLM reranker(`rerank.py`, 기존)가 카드
  텍스트를 *읽고* 최종 판단** → 임베딩 OOD를 교정. 즉 **recall=high-recall, 변별=LLM**.

**우려 ② LLM 비용**: 매 홉·매 노드 LLM(naive ToG)은 비싸다(≈ 축4 × 홉 × beam → 30–50콜,
비-harvester 비용 ~2배). **그래서 그렇게 안 한다 — 캐스케이드**:

| 단계 | 엔진 | LLM 비용 | 역할 |
|---|---|---|---|
| 광역 recall | PPR + 임베딩 | **0** | 노드 대부분 |
| 정밀 | LLM reranker (기존) | **1콜/축** | 변별 + OOD 교정 |
| 심층(gated) | LLM 빔 (Tier 3) | gap축만·배치·PoG 조기정지 → **~3–6콜** | 불확실 frontier만 |

- **escalation hook (Tier 2.5, 작은 추가)**: 임베딩 margin 낮음 / 신규 promote Entity / 최근 doc →
  "불확실" 표시해 rerank 후보에 **강제 포함**(임베딩이 조용히 드롭하지 않게). **LLM 추가콜 0.**
- **결론**: 매 홉 LLM은 불필요. 임베딩으로 넓게 회수 → LLM은 *정밀 1콜/축* + *불확실 frontier*에만.
  비용은 `O(축 × 라운드)`이지 `O(노드 × 홉)`이 아니다. (PoG가 ToG 대비 콜 ~40%↓.)

---

## 6. 혼합 출처 그래프 — 웹검색 + 정형 API 통합

> 추가 배경(2026-07-01): 이 시스템은 "웹전용"이 아니라 **웹검색-우선 + 정형 API 보강**이다.
> 음악도 실시간 차트·스트리밍 등 웹검색에 안 나오는 데이터를 외부 API로 더할 계획이고,
> 부동산은 웹에 없는 API/관계형 데이터가 다수다(`real_estate/database` SQLite). 탐색(PPR/walk)
> 설계가 이 **혼합 출처**를 처음부터 가정해야 Tier 1/2가 web-only 가정에 갇히지 않는다.

### 6.1 원칙 — 수집 ≠ 적재 ≠ 탐색 (3계층 분리)
| 계층 | 무엇 | 자산별/공통 | 출처 차이가 사는 곳 |
|---|---|---|---|
| **수집(Collection)** | 웹검색 / 정형 API / 관계형 DB | **자산별** (web=infra 공통, 정형=자산 `external/`) | 채널마다 다름 |
| **적재(Loading)** | typed 노드 + Fact + provenance 엣지 | 공통 계약 | `source_kind`·`source_tier`·`valid_*` 태그 |
| **탐색(Retrieval)** | PPR / walk | 공통 | **출처 무관** — node_score(tier·validity)·엣지 prior로만 차등 |

핵심: **그래프는 적재 시점에 출처를 태깅하되, 탐색 엔진은 출처를 모른다.** PPR/walk는 균일한
노드 위를 돌고, "API데이터냐 웹이냐"는 오직 `node_score`(신뢰도·최신성)와 엣지 prior(`profiles`)
로만 반영된다. → **채널을 추가해도 탐색 코드는 안 바뀐다**(당신의 "공통 구조" 목표 보존).

이 seam은 **이미 절반 존재**한다:
- `seeder._collect_structured`가 정형 커넥터를 seed-time에 typed 노드로 적재하고, 식별자-신뢰라
  보고서 증거로 직접 렌더된다(웹 Fact의 confidence 0.68과 구분).
- `seeder._collect_social`은 정형 출처(Genius About)도 **웹 Fact와 동일 provenance 체인**
  (Fact→WebDoc→MENTIONS)으로 적재 — 즉 적재 계약은 이미 출처 무관.
- `policies.StaticPolicy.node_score`(`policies.py:73`)는 `confidence`와 WebDoc `source_tier`를
  곱한다 — 출처 차등의 자리가 이미 있다.

### 6.2 적재 *방법* — 균일 계약 + 출처 태깅
모든 채널(웹/API/DB)이 같은 형태로 적재된다:
- **typed 노드** (Chart/SocialMetric/Award/… — 스키마는 출처 무관),
- **Fact** (+ `source_kind ∈ {web, structured_api, relational_db}`, + 추적용 출처): 웹은
  verbatim `source_excerpt`(grounding 계약), API/DB는 식별자-신뢰(응답 필드·엔드포인트·행키),
- **provenance 엣지**: 웹은 `Fact-EXTRACTED_FROM->WebDoc`; API는 `Fact-EXTRACTED_FROM->ApiSnapshot`
  (신규 노드) 또는 WebDoc에 `source_kind=api` 태그 — 최소변경 택1,
- **신뢰도**: API/DB는 `source_tier≈1.0`(식별자 기반), 웹은 harvester가 채점한 tier.

→ 부동산은 이 방법을 **이미** 쓴다(`seeder.collect_scope`가 SQLite를 scope로 컴파일). 음악
실시간 API도 같은 **Loader 계약**으로 들어오면 PPR이 그대로 회수한다.

### 6.3 적재 *타이밍* — 앵커 먼저, 웹 누적, 휘발성 갱신
1. **seed-time (앵커)**: 안정적 정형 데이터(식별자·구조·권리자)를 먼저 적재 → typed 노드가
   **앵커**가 되고 harvester가 웹 mention을 여기에 링크(`_match_scope_name`). *순서가 중요* —
   구조가 먼저 있어야 웹 증거가 붙을 못을 가진다. (현행 그대로)
2. **harvest accretion**: 웹 Fact가 앵커 주변에 누적(현행 `expand`).
3. **휘발성 갱신 (신규)**: 실시간/시변 데이터(차트 순위·스트리밍·공실률)는 **스냅샷 + valid_from/
   valid_until·최신성**으로 적재. run마다 **staleness-gated 재수집**(부동산 `tenant_collector`
   90일 게이트와 동형). 과거 스냅샷은 보존 → 시계열이 `sustainability` 축의 증거(SocialMetric이
   이미 "스냅샷" 모델).

### 6.4 탐색이 혼합을 다루는 법 (PPR/walk `node_score` 확장)
혼합 출처는 PPR에 **두 항**으로만 들어온다(엔진 불변):
- **신뢰도 prior(tier)**: `node_score *= source_tier`. 고신뢰 API 노드는 PPR 점수/시드 질량을 더
  받아 웹 소문에 안 묻힌다.
- **최신성/유효성(validity)**: `policies.py`의 `node_score` TODO(시점 감쇠)를 **이제 구현**한다 —
  부동산 `validity.py`(half-life decay·valid_until)를 `infra`로 일반화. 실시간 차트가 생기며
  음악에도 *필수*가 됨. stale 스냅샷은 PPR에서 자연 감쇠.
- **엣지 prior는 이미 출처를 간접 차등**: 정형 결과 엣지(CHARTED_ON 등)는 `profiles`에서 축별로
  다르게 흐른다(social_value가 상업 엣지를 누르는 것과 동일). → API 데이터가 무조건 우대받지
  않고 *축 적합성*에 따라 흐른다.

### 6.5 이번 Tier 1/2에 미치는 영향 (지금 할 일)
- Tier 1/2 코드는 **출처를 몰라도 된다**(원칙 §6.1) → **추가 작업 없이 혼합 그래프에서 동작**.
- 단 두 가지를 *지금* 반영해 나중에 PPR/walk를 다시 안 건드리게:
  - **(a) tier 항**: PPR `node_score`에 `source_tier` 곱을 **처음부터** 넣는다(§5.2 시드·전이).
  - **(b) validity 자리**: `node_score(label, props)`에 `valid_until`을 읽는 **인터페이스만 뚫어
    둔다**(감쇠 함수는 실시간 API 도입 시 채움). 지금 안 뚫으면 나중에 양쪽 정책을 다시 수정.
- **별도 설계 예고**: 정형/실시간 *수집·적재 메커니즘*(Loader 계약·`ApiSnapshot` 스키마·refresh
  스케줄·부동산 DB↔graph 타이밍)은 `docs/design/structured_ingestion.md`(신규)로 분리한다. 본
  문서는 *탐색*이 혼합을 다루는 지점까지만 고정한다.

---

## 7. 공통 구조 (infra 승격) — 두 자산 공유

당신의 1순위 목표("최대한 공통 구조로 전 자산")에 맞춰:
- **PPR 엔진**은 도메인 무관 → `infra/graph/ppr.py`로 둔다(walk가 `infra.graph.walk`인 것과 동형).
  자산은 scope·seed·전이 prior만 주입.
- **라우터**의 깊이 정책도 자산 무관 메커니즘 → `infra`에 두고 축 목록·coverage만 주입.
- `WalkPolicy`가 이미 infra 프로토콜이듯, **`RecallStrategy`**(walk|ppr|hybrid) 인터페이스를
  infra에 정의하면 Tier 3 에이전트 전략도 같은 seam에 꽂힌다.
- 검증: `tests/characterization/`에 "두 자산 compose가 recall.mode 분기에서 동일 계약"
  스모크 추가(`asset_layering.md` §4 패턴).

> 단, 이번엔 **음악에서 먼저 구현·검증**하고(골든 작품 회귀 확보) 그 다음 infra 승격 →
> 부동산 적용한다. 미러 원칙상 음악 `composer`/`config`에 먼저 넣고, 안정화 후 `infra`로 추출.

---

## 8. 마이그레이션 · 플래그 · 롤백

- **기본 불변**: `recall.mode=walk` + `adaptive.enabled=false` → Tier 0과 바이트 동일. 신규 코드는
  전부 플래그 뒤(담론 `curation.mode`와 동일 규율).
- **점진 활성**: ① `adaptive.enabled=true`(Tier1)만 먼저 → ② `recall.mode=hybrid`(Tier2) → ③
  안정 시 `ppr`.
- **롤백**: 플래그 1개 되돌리면 즉시 레거시 경로. PPR/라우터 실패는 best-effort로 walk 폴백
  (scope 비었거나 임베딩 없음 → `logger.debug` 후 walk).
- **best-effort 계약**: §외부호출 규약과 동일하게 신규 단계가 보고서 생성을 깨지 않는다.

---

## 9. 검증

1. **골든 작품 회귀**: Dynamite(자산 A-/사회 B 기검증, `music-social-value-axis`) +
   LOVE ATTACK(RESCENE) 등으로 Tier0 vs Tier1 vs Tier2(`recall.mode` hybrid/ppr ×
   `edge_prior` profile/semantic) 보고서 A/B.
   - 측정: 축별 evidence 수·source_kind 분포(`report.json` audit 블록), 등급 안정성, 사회축
     근거의 비-상업 비율(손튜닝 억제가 `edge_prior=semantic` 계산값으로 재현되는지 = 핵심 검증).
2. **결정성 체크**: PPR recall 단독(rerank 끔)으로 동일 입력 2회 → 동일 순위.
3. **비용**: run당 LLM 호출 수·wall-clock(Tier2는 LLM 0 증가가 목표; Tier1 휴리스틱도 0).
4. **웹전용 시나리오**: 정형 커넥터 끈 시드(웹수집만)에서 Tier0 대비 사회·지속성 축 recall 개선
   확인(이 작업의 본 목적).
5. 단위: `ppr.py` PPR 수렴·합보존·dangling 처리 테스트(`music_copyright/tests/`).

---

## 10. 범위 밖 (후속 Tier 로드맵)

- **Tier 3 (ToG/PoG 에이전트 빔)**: gap-flagged 축에만 발동하는 `infra/graph/agentic_walk.py`.
  매 홉 `(target_card, 후보 카드, 관계타입, 축의도)`를 LLM에 주고 beam 선택 + reflection(충분?/
  계속?/백트랙?). `_expand`(`traversal.py:36`) 재사용. **이것이 "평가대상↔임의 노드 관계를 LLM이
  판단해 확장"의 완성형.** PoG로 LLM 호출 40%↓.
- **Tier 4 (GFM-RAG/G-Reasoner)**: 학습된 GNN retriever, 음악+부동산 통합(QuadGraph식). 논문화
  후 별도 트랙.
- **적재 측(EDC/AutoSchemaKG)**: 본 문서는 *탐색*만 다룬다. 적재 고도화(EDC Define 단계 정식화,
  AutoSchemaKG 동적 스키마)는 별도 설계(`docs/design/` 신규)로.

---

## 11. 참고문헌

- **HippoRAG 2** — *From RAG to Memory: Non-Parametric Continual Learning for LLMs*, arXiv:2502.14802 (ICML 2025).
- **Adaptive-RAG** — *Learning to Adapt Retrieval-Augmented LLMs through Question Complexity*, arXiv:2403.14403 (NAACL 2024).
- **Plan-on-Graph (PoG)** — *Self-Correcting Adaptive Planning of LLM on Knowledge Graphs*, arXiv:2410.23875 (NeurIPS 2024).
- **Think-on-Graph** — arXiv:2307.07697 (ICLR 2024) · **ToG 2.0** — arXiv:2407.10805 (ICLR 2025).
- **Graph-CoT** — *Augmenting LLMs by Reasoning on Graphs*, arXiv:2404.07103 (ACL 2024 Findings).
- **GFM-RAG** — arXiv:2502.01113 (NeurIPS 2025) · **G-Reasoner** — arXiv:2509.24276 (2025).
- **EDC** — *Extract, Define, Canonicalize*, arXiv:2404.03868 (EMNLP 2024) [적재 측, 별도 설계].
- GraphRAG 서베이 — arXiv:2408.08921, arXiv:2501.00309.
