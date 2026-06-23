# 실험: writer 모델 × 큐레이션 모드 A/B

- **일시**: 2026-06-23
- **대상**: 서울특별시 강남구 강남대로 396 (강남역지하도상가, `asset_id=re:1168010100108580000000013`)
- **목적**: ① 집필(writer) 모델을 낮추면 보고서 품질이 얼마나 떨어지나 ② STORM(담론) 큐레이션이 static 대비 보고서를 개선하나 ③ 담론 아웃라인이 싼 writer를 구제하나
- **심판**: Claude (지표 추출기 + 동일 대목 원문 비교, LLM감사=sonnet 고정으로 cross-cell 일관)

## 설계 — 2×3 factorial

| | writer=sonnet | writer=haiku | writer=gpt_mini |
|---|---|---|---|
| **static** | 셀1 (베이스) | 셀2 | 셀3 |
| **discourse(STORM)** | 셀4 | 셀5 | 셀6 |

**통제**: writer A/B는 *동일 그래프*에서 재집필(`expand_rounds=0` 또는 write-only 재호출)이라 **수집 증거가 1bit도 안 변하고 writer 모델만 변수**. 수집(harvester/researcher 등)은 writer와 무관한 고정 티어 — 이것이 통제의 핵심.
static→STORM은 그래프 wipe 후 동일 주소 재실행(같은 시작점, 큐레이션 모드만 차이).

---

## Phase 1 — writer A/B (static, 동일 1174-사실 그래프)

| 지표 | sonnet | haiku | gpt_mini |
|---|---|---|---|
| 종합등급 | B+ | B | B+ |
| 임차안정성 등급 | B | **C-** | C+ |
| 주장 수 | **73** | 58 | **30** |
| 해석(opinion) 주장 | **20** | 6 | 3 |
| 인용 근거 | 21 | 17 | 10 |
| 본문 분량 | ~7,000자 | ~4,500자 | ~3,300자 |
| 기계감사 잔여위반 | 1 | **4** | 1 |
| LLM감사 high | 2 | 2 | **3** |
| 공실률 6.3% vs 18.9% 상충 | **명시·다출처 화해** | **드롭(무시)** | **드롭(무시)** |

**발견**: 싼 모델일수록 ① 주장·해석·근거가 급감(73→58→30, opinion 20→6→3) ② **상충/미묘한 증거를 버려서** 깔끔해짐(데일리안 18.9%를 sonnet만 다룸) ③ haiku는 grounding 규율 저하(기계위반 4), gpt_mini는 과단정 심화(high 3) ④ 동일 데이터인데 임차안정성 등급이 B/C-/C+로 흔들림.

**판정**: 비싸게 모은 풍부한 증거(특히 상충·미묘)는 **강한 writer만 끝까지 활용**(메모리의 "깊이 = 데이터 × 모델" 벽 재현). **writer엔 sonnet이 값을 함.** gpt_mini는 너무 얕음(반토막). haiku는 ~80% 옵션.

---

## Phase 2 — static vs STORM (동일 sonnet writer)

| 지표 | static | STORM(discourse) |
|---|---|---|
| 종합등급 | B+ | B+ |
| 축 구조 | 고정 3축 | **+발견관점 2**(앵커평판·지하특수성), **ESG→물리/환경(E) 분리** |
| 주장 | 73 | **92** (+26%) |
| 해석(opinion) | 20 | 24 |
| **공백명시(gap)** | 8 | **17** (+113%) |
| 인용 근거 | 21 | 24 |
| 본문 분량 | ~7,000자 | ~8,800자 |
| 기계감사 | 1 | 3 |
| LLM감사 high | 2 | 2 |
| 소요 | ~46분 | **63.5분** |

**STORM이 더한 것**: Perspectivist가 `앵커 테넌트 리뷰 평판`·`지하 상권 유동·접근성 특수성` 발견 → Researcher 표적질의(`의원 공실`, `명품 폐점` 등) → Moderator 2라운드 GAP 메우기. 결과: ESG를 *물리성능 C+* vs *ESG환경(E)=D*로 분리해 인증 전무 약점을 드러냄, **gap 정직 2배**.

**판정**: 같은 등급(B+)이나 **더 풍부·정직·세분화**. 단 1.4배 시간. → 고부담 정밀 보고서엔 담론 깊이가 값을 하고, 속도·재현성엔 static(설계 의도 D20).

---

## Phase 3 — 담론 아웃라인이 싼 writer를 구제하나

담론 그래프(1299사실) + 재구성 아웃라인(esg 7·commercial 6·tenant 6 논점)으로 재집필. **Phase 1(static·아웃라인 없음)과 비교:**

| | static (Phase 1) | discourse (Phase 3) | 변화 |
|---|---|---|---|
| **haiku** 등급 | B | **B+** | ↑ (sonnet 수준) |
| haiku 기계위반 | 4 | **1** | ↓ (grounding 회복) |
| haiku ESG 섹션 | 붕괴(3블록 672자) | **7블록**(아웃라인 강제) | 구조 회복 |
| **gpt_mini** 등급 | B+ | B | ↓ |
| gpt_mini 섹션 | 1블록 붕괴 | **여전히 1블록 붕괴** | 구제 실패 |
| gpt_mini | — | 호선 오류 등 사실오류 | 악화 |

**발견**: **아웃라인은 haiku를 구제, gpt_mini는 못 구제.**
- **haiku**: 아웃라인이 *조직화·grounding 규율*을 대신 줘서 static에서 못 넘던 B+(=sonnet 등급) 도달, 기계위반 4→1. **중간 모델은 스캐폴드로 살아남음.**
- **gpt_mini**: 6-7논점 아웃라인을 줘도 **1블록으로 뭉개고**(스캐폴드 무시) 사실오류까지. **너무 약한 모델은 스캐폴드도 못 씀.**

이는 설계 가설(static=writer에게 더 어려운 시험)을 입증 — haiku는 static 실패·discourse 통과(=구제됨), gpt_mini는 둘 다 실패.

---

## 최종 결론 — 전체 6셀 매트릭스

| writer \ mode | **static** | **discourse(STORM)** |
|---|---|---|
| **sonnet** | B+ · 73주장 · 20해석 · 기계1 · high2 | B+ · 92주장 · 24해석 · 기계3 · high2 |
| **haiku** | B · 58 · 6 · 기계4 · high2 | **B+ · 55 · 6 · 기계1 · high2** ◀ 구제됨 |
| **gpt_mini** | B+ · 30 · 3 · 기계1 · high3 | B · 28 · 2 · 기계0 · high2 |

**실무 권고:**
1. **static 모드 → writer는 sonnet** — 스캐폴드 없어 싼 모델이 깊이·grounding 모두 무너짐.
2. **discourse 모드 → haiku writer 가능** — 담론 아웃라인이 haiku를 ~sonnet 품질로 끌어올림(B+·기계위반1). **writer 비용 절감 = discourse일 때 성립.** ← 사용자 비용 우려에 대한 실측 답
3. **gpt_mini는 writer로 비권장**(두 모드 다 너무 얕음·사실오류).
4. **데이터 두께의 가치는 모델에 비례** — 1174~1299 사실의 풍부함은 강/중 모델만 활용. gpt_mini는 ~30주장으로 절반 이하만 씀.

---

## 셀 인덱스
- `cells/1_static_sonnet/` — 베이스라인
- `cells/2_static_haiku/`, `cells/3_static_gptmini/` — Phase 1
- `cells/4_discourse_sonnet/` — STORM (Phase 2)
- `cells/5_discourse_haiku/`, `cells/6_discourse_gptmini/` — Phase 3
- 각 셀: `report.md`(렌더), `report.json`(구조+감사+등급)

## 부수 발견 (실험 중 관찰)
- **병목**: 수집 시간의 ~84%가 **순차 LLM 사실추출**(gpt-4o-mini, 529호출×4.1s). ddgs는 채널당 최악(게이트웨이 1.5s)이나 전체 ~15%. → 1순위 레버 = harvest 문서별 추출 **병렬화**.
- **아파트 예산 누수**: navigator가 배후수요 아파트(Complex)에 ~10% 예산 소비 → COMPLEX 우선순위 0.2→0.1 + `시세` 템플릿 제거(재건축만)로 STORM부터 완화 적용.
- **저장 quirk**: `try_full_report`가 보고서를 `reports/report.json`에 loose 저장(run 디렉터리 아님) — 덮어쓰기 위험, 수정 필요.

## 재현 / 추적
- 지표: `python report_metrics.py cells/<셀>/report.json "<라벨>"`
- run_id: static sonnet `rpt_131749_afaca95f` · haiku `rpt_140543_7e64927a` · gptmini `rpt_141038_5b0b1146` / STORM sonnet `rpt_152757`(sidecar `full_142555_c6292104`) · p3haiku `p3haiku_153823` · p3gptmini `p3gpt_mini_154336`
- 전 사이드카(trace·llm_calls·tool_calls): `real_estate/database/DB/reports/<run_id>/`
- LLM감사(심판)=sonnet 고정 → 6셀 cross 일관 비교
