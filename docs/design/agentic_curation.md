# 담론 주도 큐레이션 — STORM/Co-STORM 기반 에이전트 구조 고도화

> 상태: **✅ 구현됨(2026-07-01)** — STORM/Co-STORM 담론 큐레이션 구현·병렬화 완료(메모리 `storm-parallel-plan`). 이 문서는 설계 근거로 보존. 결정 로그 D17~D21 (graph_native_flow.md).
> 목적: 현재 보고서 흐름의 "에이전틱하지 않음"을 해소 — 고정 EXPAND를 관점 기반
> 담론 루프로 고도화하되, **그래프·walk·writer·재현성은 그대로** 둔다.

---

## 0. 동기 — 지금 왜 "에이전틱하지 않은가"

현재 흐름: `SEED → TENANT → EXPAND(navigator→harvester, 고정 라운드) → [3축 병렬 write] → SYNTH → FINALIZE`.

자율 결정은 walk_judge(탐색 깊이)·qa(자가확장) 둘뿐, 나머지는 **고정 DAG 안에서 schema를 채우는** 구조다. 구체적 한계:

- **관점이 하드코딩** — tenant/esg/commercial 3축 고정. 건물유형(오피스/근린상가/복합)별로 평가해야 할 각도가 다른데 못 살린다.
- **수집이 1시점 1패스** — navigator가 frontier에서 검색어를 뽑고 harvester가 1회 추출. "찾은 것에서 다음 질문이 나오는" 심화 루프가 없다.
- **공백을 사냥하는 주체가 없음** — auditor는 모순/과대해석만 본다. "무엇이 안 다뤄졌나(unknown unknowns)"를 능동적으로 찾는 에이전트가 없다. (auditor notes→GAP→EXPAND 재진입은 설계상 미배선.)
- **구조가 담론에서 안 나옴** — 아웃라인 없이 고정 3축으로 바로 집필.

## 1. 논문 근거 (STORM / Co-STORM)

**STORM** (Stanford OVAL) — 사전집필을 둘로:
1. **관점 발견(Perspective discovery)**: 유사 주제 글들을 훑어 다양한 관점을 발견, 질문의 앵커로.
2. **관점 기반 Q&A + 시뮬레이션 대화**: "글쓴이 페르소나"가 "전문가 페르소나"에게 질문 → 전문가는 **검색으로 근거를 찾아** 답 → 후속 질문으로 심화.
3. 대화 → **계층 아웃라인** → 섹션별 집필.

**Co-STORM** (*Into the Unknown Unknowns*, arXiv 2408.15232) — STORM + 협업 담론:
- **전문가 에이전트들**(서로 다른 관점) + **모더레이터**(비전문가지만 좋은 질문으로 담론을 조종; *인용 안 된 소스·미탐색 영역에서* 질문을 던져 unknown unknowns 발굴).
- **동적 마인드맵**: 담론 정보를 계층 구조로 조직 → 보고서.

## 2. 매핑 — Co-STORM 마인드맵 = 이미 보유한 그래프

| STORM/Co-STORM 요소 | 현재 시스템 | 격차 |
|---|---|---|
| 동적 마인드맵 | **Neo4j 그래프 — 보유 (우위)** | ✅ 큰 자산 |
| 관점 발견 | 고정 3축 | ❌ 발견 아닌 하드코딩 |
| 관점별 질문 | navigator 1시점 + 코드 가드 | ❌ 다관점 없음 |
| 시뮬레이션 Q&A(다턴) | harvester 1회 추출 | ❌ 후속 질문·심화 없음 |
| 모더레이터(unknown unknowns) | auditor(모순만) | ❌ 커버리지 공백 사냥 부재 |
| 담론→아웃라인 | 고정 3축 | ❌ 구조가 담론에서 안 나옴 |

**핵심**: Co-STORM이 어렵게 만드는 "동적 마인드맵"을 그래프로 이미 갖고 있다. 빠진 건 그 맵을 **에이전틱하게 채우는 담론**이다.

## 3. 제안 아키텍처 — 담론 주도 큐레이션 (EXPAND 고도화)

고정 EXPAND를 **discourse loop**로 교체/증강. 새 에이전트 4종 + 기존(walk·harvester·그래프) 재사용.

```
TENANT(건물유형 분류 — 이미 보유)
   │
   ▼
P1 Perspectivist ──► 관점 N개 발견 (건물유형 기반)
   │
   ▼   ┌──────────── discourse loop (모더레이터가 종료 판정) ───────────┐
   │   │  P2 Questioner(관점) ──질문──► Researcher ──walk+harvester──►   │
   │   │        ▲                          │ (새 Fact를 그래프=맵에 적재) │
   │   │        └──후속 질문(심화)◄────────┘                              │
   │   │  P3 Moderator: 그래프 커버리지 측정 → 빈 관점/미인용 영역에      │
   │   │                새 질문 주입 (unknown unknowns). 충분하면 종료.   │
   │   └───────────────────────────────────────────────────────────────┘
   ▼
P4 Outliner ──► 담론+그래프 → 계층 아웃라인
   │
   ▼
Writer(아웃라인 노드별 집필 — grounded, ① 깊이 캡 완화) → SYNTH → FINALIZE
```

### P1. Perspectivist (관점 발견) — STORM 관점 단계
- **3축 = 고정 척추 (버리지 않음).** tenant/esg/commercial은 구조화 데이터 소스에 1:1 매핑(tenant←tenants/turnover/reb_lease, esg←building_register, commercial←commercial_sales/area)되므로 결정적으로 유지 — 보고서 목차의 뼈대.
- **발견 관점 = 추가 보충 (additive, 상한 N).** Perspectivist는 building_type 기반으로 *추가* 관점을 발견(오피스면 "입주사 신용도/교통 호재", 근린상가면 "배후 아파트 입주"). 부모 축에 붙여 심화하거나 독립 '특이사항' 섹션으로. **3축을 대체하지 않고 얹는다.**
- **발견 관점은 검색으로만 채운다 (D22).** 구조화 소스는 3축에 고정 배선돼 있고 LLM-callable 도구가 아니므로, 추가 관점의 증거는 Questioner→Researcher→harvester(검색)가 공급. 이것이 도구 현실에 맞는 유일한 자율 획득 경로.

> **도구 현실 (D22) — 왜 완전 자율이 아닌가.** 자율 구동 가능한 데이터 획득은
> 검색(naver/web)뿐이다. 구조화 소스(building_register·tenants·turnover·
> commercial_sales·reb_lease)는 seed/tenant에 고정 배선된 명령형 호출이고
> bind_tools(LLM 함수호출)는 시스템에 없다. 자산당 구조화 데이터는 유한·고정이라
> '도구 선택' 여지가 작다 → **3축=구조화 척추(결정적) + 검색 기반 발견 관점
> (에이전틱·보충)**. 완전 tool-agent(repository→BaseTool+bind_tools)는 ROI가 낮아
> 보류. 에이전틱 가치는 도구 라우팅이 아니라 *무엇을 검색할지·언제 충분한지*에 있다.

### P2. Questioner ↔ Researcher (시뮬레이션 Q&A) — STORM 대화 단계
- **Questioner**(관점 페르소나): 관점에 비춰 답이 필요한 질문 생성. navigator의 frontier-walk를 *질문 주도*로 대체.
- **Researcher**(전문가): 질문을 walk(그래프) + harvester(웹/뉴스)로 답. 답변 근거 Fact는 **그래프에 적재**(맵 성장).
- 다턴: 답에서 후속 질문 도출(심화). 턴 예산은 관점별 cap.
- 재사용: walk 엔진·harvester·link_mention(오염 방지)·validity 전부 그대로.

### P3. Moderator (unknown unknowns) — Co-STORM 모더레이터
- **그래프 커버리지 메트릭**으로 구동(§4): 관점별 Fact 밀도, 인용 안 된 노드, 만료 임박, 빈 영역.
- 빈 곳을 메우는 새 질문을 담론에 주입. 모든 관점이 임계 커버리지에 도달하거나 novelty가 마르면 **종료 판정**(LLM-decides-flow).
- auditor(모순 탐지)와 **역할 분리**: moderator=커버리지/공백, auditor=정합성. moderator가 오래 미뤄둔 "GAP→미니EXPAND 재진입"을 원리적으로 배선(D7 QA 자가확장의 일반화).

### P4. Outliner (담론→아웃라인) — STORM 아웃라인 단계
- 큐레이션된 그래프 + 담론 로그 → 계층 아웃라인(관점→하위 논점).
- Writer가 고정 3축이 아니라 **아웃라인 노드별**로 집필. ①(깊이 캡 완화)과 시너지 — 논점이 많을수록 더 깊고 길게.

## 4. 그래프 = 마인드맵 (커버리지 신호)

모더레이터가 "어디가 비었나"를 **측정 가능**하게 만드는 게 그래프의 핵심 가치. 후보 메트릭(전부 Cypher로 산출):
- 관점별 연결 Fact 수 / distinct press 수(corroboration).
- 자산 스코프 내 미인용(어떤 claim도 안 가리킨) 노드 비율.
- 만료 임박/DECAYED 증거 비율(신선도 공백).
- 관점 간 불균형(어떤 관점만 증거 풍부, 나머지 빈약).

→ moderator는 이 신호로 다음 질문의 타깃을 고르고 종료를 판정. 커버리지 점수는 Langfuse score로도 적재 가능(품질 시계열).

## 5. 신규 vs 재사용 vs 변경

| 구분 | 항목 |
|---|---|
| **신규 에이전트** | Perspectivist · Questioner · Researcher · Moderator · Outliner (전부 BaseAgent) |
| **재사용(불변)** | 그래프·walk 엔진·StaticPolicy/SemanticPolicy·harvester·link_mention·validity·composer·writer·auditor·QA/revise |
| **변경** | EXPAND 노드 → discourse loop로 교체(flag 뒤); writer가 아웃라인 입력 수용; workflow.py 그래프 배선 |
| **모델 매핑** | Questioner/Moderator=haiku(저단가 다턴), Researcher=기존 harvester tier, Perspectivist/Outliner=sonnet — 전부 config.json agents에 추가, A/B 대상 |

## 6. 재현성 — variant/flag 뒤 (D4·D20 일관)

보고서의 **StaticPolicy 재현성은 핵심 가치**(D4). 담론 루프는 비결정적이므로:
- `config.json`에 `curation.mode: "static" | "discourse"` 플래그. 기본 `static`(기존 결정 경로). 실험은 `discourse`.
- `set_active_override`로 A/B variant에 묶임 — STORM 경로 vs 기존 경로를 벤치마크 dataset에서 비교(비용·깊이·커버리지).
- 프로덕션 의뢰 보고서는 합의 전까지 `static` 유지.

## 7. 구현 단계 (PR 단위, 점진)

| 단계 | 범위 | 산출 |
|---|---|---|
| PR-1 | 커버리지 메트릭 모듈(Cypher) + Langfuse score 적재 | moderator의 측정 토대. 기존 흐름 불변 |
| PR-2 | **Moderator** + GAP→미니EXPAND 재진입 (flag 뒤, 기존 EXPAND에 얹음) | 가장 큰 에이전틱 이득을 먼저 |
| PR-3 | Perspectivist + Questioner/Researcher discourse loop | EXPAND 교체(flag 뒤) |
| PR-4 | Outliner + writer 아웃라인 입력 | 구조의 동적화 |
| PR-5 | A/B 벤치마크(static vs discourse) — 비용·깊이·커버리지 | variant 검증 |

> 각 PR은 flag로 격리 → 기존 결정 경로가 항상 작동. PR-2만으로도 독립 가치(미뤄둔 GAP 루프 완성).

## 8. 리스크 / 미해결

- **비용·지연**: 다턴 담론은 호출 수↑. haiku 다턴 + 턴 예산 cap + novelty 종료로 통제. A/B로 비용 곡선 측정 필수.
- **무한 루프**: moderator 종료 판정이 약하면 발산. 하드캡(턴·예산) + novelty 마름 이중 종료.
- **관점 폭발**: Perspectivist가 관점을 너무 잘게 쪼개면 산만. N 상한 + 3축 카테고리로 묶기.
- **측정 기준**: 깊이·커버리지의 "좋음"을 무엇으로 재는가는 별도 결정(현재 보류). 일단 분리축(variant/score)만 확보.
- **재현성 경계**: discourse 산출물을 보고서로 승격할 때의 버전·재현 정책 — D12 구조화 JSON과 정합 필요.

## 참고문헌
- STORM — stanford-oval/storm · https://storm-project.stanford.edu/research/storm/
- Co-STORM — *Into the Unknown Unknowns: Engaged Human Learning through Participation in Language Model Agent Conversations*, arXiv:2408.15232
