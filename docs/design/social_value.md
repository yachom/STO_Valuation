# 사회적 가치 평가 — 통합 설계 & 코드/프롬프트/제약 변경안

> 작성일: 2026-07-01 · 대상: `music_copyright` + `real_estate` · 우선: 음악 먼저
> 배경: **이 레포의 핵심 목적은 STO 기초자산의 「사회적 가치」 평가 보고서**(memory `repo-purpose`).
> 그런데 현재 사회가치는 *과소·부정확*하게 취급된다(코드 감사). 이를 문헌 기반으로 재정초한다.
> 근거: 음악 사회·문화가치 프레임워크 리서치 + 부동산 사회가치 프레임워크 리서치 + 코드 감사.
> 관련: [`agentic_traversal.md`](agentic_traversal.md)(탐색=사회근거 회수 수단), `structured_ingestion.md`(예정).

---

## 0. 한 문단 요약

사회적 가치를 *catch-all 의도문(음악)·상업지표의 별칭(부동산)*에서, **문헌에 근거한 측정가능한
차원(dimension)의 합성 지표**로 재정의한다. 두 자산이 **원칙·아키텍처는 공유**하되(상업
firewall·추가성·직교성·비화폐 composite·증거 tiering·social-primary 이중트랙) **차원은 자산
고유**로 둔다(음악=문화·사회 역할, 부동산=공동체·접근성). 그래프 스키마·수집·등급·프롬프트·제약을
이 차원 구조에 맞춰 바꾼다. **핵심 경고(문헌 합의): 검증된 단위(곡·건물) 사회가치 척도는 존재하지
않는다 — 우리 지표는 정당하되 *논쟁 가능한 설계 선택*이며, 가중치·차원을 그렇게 문서화한다.**

---

## 1. 진단 — 현재 사회가치 취급의 결함 (감사 요약)

### 1.1 음악 (`music_copyright`)
- 사회축은 1급 4번째 축이나 **시딩이 Genius "About" 단일 출처**에 의존(`seeder.py:272-273,340-342`).
  About이 비면 사회 노드 0개 → 실제로 **LOVE ATTACK은 SocialTheme/Cause/PublicUse 0개**(검증됨).
- 의도(`axes.py:83-86`)가 7가지 이질적 항목(가사테마·시위앤섬·아티스트 공적행위·팬덤·대표성·
  소프트파워·공적활용)을 **가중치·임계·우선순위 없이** 묶은 catch-all. 루브릭의 유일 정량 변별이
  "다출처(A) vs 1~2건(C)".
- **work-vs-artist 모순**: 정의·등급은 "작품 ≠ 아티스트/팬덤 분리"를 반복하나, harvester는 사회축에서
  artist-alias를 곡 주어 앵커로 허용(`harvester.py:486`) → 적재층이 경계를 무너뜨림.
- 소프트파워·대표성은 정의에만 있고 **스키마·추출기 거처가 없음**(자유텍스트 Fact로만).

### 1.2 부동산 (`real_estate`)
- **독립 social_value 축이 없다.** `esg` 축은 사실상 **E-only**(에너지·녹색인증·물리상태,
  `composer.py:36`)이고 S·G가 없음. 보고서 프레임이 "사회적 가치"를 표방하나(`prompts.json:3-4`)
  내용은 **상업 지표(임차안정·상권건강·입지·배후수요)의 umbrella 별칭** — 정의적 눈속임.
- music과 달리 **독립 social_grade가 없음**(`synthesizer.py:37-47`, overall 1개로 융합).
- 사회/공동체/거버넌스 노드·엣지가 스키마에 **전무**.

### 1.3 공통
- 두 자산에서 "사회적 가치"가 **상반된 의미**(음악=상업과 격리된 좁은 축 / 부동산=상업을 사회로
  재명명). 보고서 간 비교 불능.
- 자기 문서가 스스로 "placeholder/TODO/3축"이라 표기 — 분류가 미정착.

---

## 2. 문헌이 강제하는 6대 원칙 (양 자산 공통 — 아키텍처 통합 지점)

| # | 원칙 | 근거 | 코드 함의 |
|---|---|---|---|
| **P1** | **상업 firewall** — 상업 신호(스트리밍·차트·판매 / 시세·역세권·학군)는 사회축에 *절대* 유입 금지 | Throsby·Holden·AHRC: 인기 ≠ 문화가치 / RE: 역세권·학군은 commercial | 음악: profiles 상업엣지 억제(기존) + workstream A `edge_prior=semantic` 자동억제. RE: 사회 profile에서 가격·입지 엣지 제외 |
| **P2** | **추가성(additionality)** — *법정·시장 baseline 초과분*만 사회가치 | UKGBC 원칙6 | 코드 규칙: 법정 의무(접근성 코드·의무 임대비율)·단순 존재는 0, 그 이상만 가점 |
| **P3** | **직교성(orthogonality)** — 욕망도(desirability)가 아니라 **약가격/비가격 사회재 접근 + 형평**을 잰다 | RE 더블카운팅 회피 | RE: 'equity-of-access' 우선. 음악: 곡의 사회적 *역할*을 인기와 분리 |
| **P4** | **비화폐 composite** — 단일 화폐액(SROI) 금지, **출처추적 가능한 가중 합성 점수** | RICS는 SROI 거부, CBA/wellbeing 선호 / SROI 비율 £0.79~28 편차 | 등급은 정성(A+~D) 유지, 차원별 sub-score 합성. 화폐화는 *예시 벤치마킹*만 |
| **P5** | **증거 tiering + absence≠zero** — 신호를 T1(검증가능)/T2(문서화·민감)/T3(아스피레이션)로, **근거 부재=미상(unknown)이지 0이 아님** | 음악 recency/Anglophone 편향 / RE Korea 데이터 갭 | 노드/Fact에 `evidence_tier`, 점수에 신뢰도·age-adjust. 신곡·구곡·비서구 페널티 금지 |
| **P6** | **이중트랙 + social-primary** — 사회가치를 *독립 등급*으로, 그리고 **레포 목적상 1급 결론**으로 | repo-purpose | 음악 dual-track 유지·강화. RE social_grade 신설. 보고서가 사회가치를 중심에 |

> P1+P6은 workstream A와 직결: PPR `edge_prior=semantic`이 사회 의도에 가까운 엣지(ADDRESSES/
> SUPPORTED/USED_IN)로 자연히 흐르고 상업엣지를 누르므로, **A의 탐색이 곧 B의 사회근거 회수기**다.

---

## 3. 사회가치 차원 (자산별 · 측정가능성 tier)

### 3.1 음악 차원 (현 SocialTheme/Cause/PublicUse 매핑 + 신설)
T1=레지스트리/카운트로 검증가능, T2=문서화·민감, T3=아스피레이션.

| 차원 | 정의 | 웹 지표 | tier | 현 스키마 매핑 |
|---|---|---|---|---|
| **S1 운동·시위 채택** | 식별 가능한 사회·정치 운동의 앤섬으로 *문서화된* 사용 | 명시 운동 채택(일자·언론), 검열/금지 기록 | T2 | SocialTheme/Cause |
| **S2 정체성·대표성** | 특정/소수 공동체 정체성 표명·대표성 마일스톤 | "최초의 X" 보도, 프라이드 앤섬, 연계 재단 | T2 | SocialTheme(+`kind=identity`) |
| **S3 자선·인도주의 동원** | 모금/인식제고 목적 제작·사용 + 수익 배분 | 자선싱글 지정, 수익 기부처, 모금액(출처) | T1* | Cause |
| **S4 시민·기념 기능** | 추모·기념·의례·공식 앤섬의 공적 사용 | 국장·기념식·취임식 사용, 국가/운동 앤섬 | T1 | PublicUse(+`kind=civic`) |
| **S5 교육 활용** | 정규 교육과정·교재·공개 수업안에 사용 | 교과서·커리큘럼·수업안 등재 | T1 | PublicUse(+`kind=education`) |
| **S6 웰빙·치료 연계** | 정신건강·사회결속·돌봄 응용과 연결 | 명시 임상/치료 프로토콜 사용(희귀) | T3 | (속성만) |
| **C1 헤리티지·정전(正典) 인정** | 보존·정전 기관의 공식 인정(곡 단위) | **National Recording Registry, Grammy HoF, 국가아카이브** 등재(yes/no+연도) | **T1**(최강) | **신설 HeritageRecognition** |
| **C2 세대 전이 longevity** | 수십 년 걸친 커버·재녹음·재유통 | **커버/샘플 수 + 십년 단위 span**(MusicBrainz/SecondHandSongs/WhoSampled) | T1 | **기존 COVERED_BY/SAMPLED_BY에서 계산** |
| **C4 소프트파워·문화외교** | 국가·기관의 대외 매력투사에 동원 | 공식/국가/UN 사용(인용), 외교 프로그램 | T3 | PublicUse(+`kind=diplomacy`) |
| **X 가사 사회테마 현저성** | 가사가 명시적 사회·정치 주제 언급(S1~S4 원료) | 가사DB 대조 | T1 | SocialTheme 원료 |

\* S3 모금액은 출처별 편차 큼 → 범위·provenance 저장, *기부 수익*(사회신호)과 *판매액*(상업) 분리.

**음악 핵심 추가:** ① **C1 HeritageRecognition**(가장 게이밍 저항적·웹검증 가능) ② **C2 longevity**
(기존 커버/샘플 엣지의 *decade-span* 계산 — 신규 노드 불필요). 둘 다 **정형 커넥터**로 적재
(혼합출처, [`agentic_traversal.md §6`](agentic_traversal.md)). **PublicUse는 양날**: 사회↑·자산↓
(퍼블릭도메인 포획·라이선스 제약) → 이중트랙이 정확히 이를 표현.

### 3.2 부동산 차원 (10개 · Korea 데이터 tier)
가족 A 점유·건물 / B 장소·접근(한국 데이터 최강) / C 공동체·형평·지역경제.

| 차원 | 정의 | Korea 지표(공공데이터) | tier |
|---|---|---|---|
| **3 사회 인프라 접근** | 학교·보육·의료·도서관·복지 등 생활SOC 근접/접근 | 학교·학구도·어린이집·HIRA 병의원 좌표 → 거리; **KRIHS 10분 SOC 그리드** | **T1** |
| **5 녹지·오픈스페이스 접근** | 공원·녹지 형평 접근(생물다양성=E축과 구분) | 전국도시공원 표준데이터(좌표·면적) + 도시공원법 권역기준 | **T1** |
| **7 부담가능·사회주택** | 시장 미충족 계층용 주택 + 사회혼합 | LH 공공임대 호수(시군구)+단지 API | T1(수)·T2(%) |
| **6 복합용도·생활활력** | 보행가능·다양 용도의 장소성 | 용도지역+건축물대장 주용도 → 다양성지수 | T2 |
| **4 대중교통 접근** | 도시 연결성 | 정류장 좌표 → 버퍼(**단, 역세권=상업 → 가중 최소화**) | T1*(직교성 주의) |
| **2 접근성·유니버설디자인** | 모든 능력의 사용성(코드 *초과*분=추가성) | 장애인편의법 baseline; 서울지하철 승강기만 | T2 |
| **8 안전(CPTED)** | 범죄·불안 저감 설계 | SafeMap 범죄주의구간(조밀하나 export 제한), 지역안전지수(시군구) | T2 |
| **1 점유자 건강·웰빙** | 공기·빛·쾌적의 건강지원 | WELL/Fitwel(희귀·비공개) | T3 |
| **9 공동체 결속·참여** | 소속·신뢰·주민참여 진정성 | 설문/과정 기반(주소단위 부재) | T3 |
| **10 지역경제 기여** | 지역 일자리·조달·사회적경제 | 자산단위 화폐화 부재(한국 TOMs 없음) | T3 |

**부동산 핵심:** ESG의 `esg`를 **Environmental로 명확화**하고, **독립 social_value 축 신설**.
**T1부터 구축**(생활SOC/공원/공공임대/복합용도 — 한국 점데이터+V-World 지오코딩). **역세권·학군은
상업으로 라우팅**(직교성 P3). 형평(소득·연령·장애별 접근)은 차원이 아니라 *접근 지표에 곱하는 modifier*.

### 3.3 왜 차원은 통합 안 하고 원칙/아키텍처만 통합하나
곡의 *문화·사회적 역할*과 건물의 *공동체 접근*은 실질이 다르다. 차원을 강제 통합하면 한쪽 모델을
다른 쪽에 강요. → `asset_layering.md` 철학대로 **원칙(§2)·등급아키텍처(§6)·스키마 패턴·증거 tiering은
공유**하고, **차원·커넥터는 자산 하위**에 둔다.

---

## 4. 그래프 스키마 변경

### 4.1 음악 (최소 증분)
- **신설 노드 `HeritageRecognition`** (key `recognition_id`): 레지스트리 등재(기관·연도·유형). C1.
- **신설 엣지 `RECOGNIZED_BY`** (Work→HeritageRecognition); `EDGE_ENDPOINTS`·`EDGE_DESCRIPTIONS`
  ("곡의 보존·정전 기관 공식 인정")·profiles social_value(고가중)·auto_commit(객관·저게이밍이라
  agentic_graph 자동커밋 후보) 등록.
- **속성 추가**: `SocialTheme.kind ∈ {agenda, identity, representation}`, `PublicUse.kind ∈
  {civic, education, commemoration, diplomacy}` — 소프트파워·대표성에 거처 부여(신규 노드 회피).
- **C2 longevity**: 신규 스키마 0 — 기존 `COVERED_BY`/`SAMPLED_BY`의 *연도 span*을 coverage/social
  점수에서 계산.
- 모든 사회 Fact에 `evidence_tier`(T1/T2/T3) 속성(P5).

### 4.2 부동산 (신규 — 현재 사회 노드 전무)
- 신설 노드: `SocialInfra`(유형: school/childcare/health/library/park/welfare), `AffordableHousing`,
  `GreenSpace`, `AccessMetric`(계산된 거리·형평). 신설 엣지: `NEAR`/`SERVES`/`PROVIDES`(Asset↔노드).
- 신설 profile `social_value`(접근·형평 엣지 고가중, 가격·역세권·인증 억제).
- (Phase 2 — RE는 관계형 DB층이 있어 커넥터가 큼. §9 참조.)

---

## 5. 수집(적재) 변경 — 다출처 + 혼합출처

### 5.1 음악 — Genius 단일출처 탈피 (가장 시급, 1.1의 0-노드 문제 직접 해소)
1. **가사 사회테마 추출(X)**: 가사 텍스트 → SocialTheme(`kind`) — grounding 계약 유지(verbatim
   excerpt). (가사 출처 확보 필요 — 라이선스 유의.)
2. **레지스트리 커넥터(C1)**: NRR/Grammy HoF/국가아카이브 → HeritageRecognition. 정형·식별자신뢰
   (source_tier≈1). 혼합출처 §6 방식.
3. **커버/샘플 longevity(C2)**: MusicBrainz `work_derivatives`(이미 보유!)·SecondHandSongs/WhoSampled
   → COVERED_BY/SAMPLED_BY + 연도 → decade-span.
4. **웹 floor 질의 강화**: `navigator._social_query_seeds`의 빈약한 `"<곡> <아티스트> 사회적 가치"`를
   차원별 질의(운동·기부·교육·기념·대표성)로 확장.

### 5.2 부동산 — 한국 공공데이터 T1 커넥터(Phase 2)
- 생활SOC(학교·학구도·어린이집·HIRA), 도시공원, LH 공공임대, 용도지역/건축물대장 → SocialInfra/
  GreenSpace/AffordableHousing/복합용도. **V-World 지오코더(주소→좌표)가 키스톤.** KRIHS 10분
  그리드를 ready proxy로.

---

## 6. 등급 아키텍처 — social-primary 이중트랙 (P6)

- **음악**: 기존 `OverviewDraft`(asset_grade/social_grade/overall, `synthesizer.py:46-69`) 유지하되,
  social_grade 루브릭을 **§3.1 차원 구조 + tier 신뢰도**로 교체(현 "다출처 vs 1~2건"의 모호함 제거).
- **부동산**: `OverviewDraft`에 **social_grade 신설**(현재 단일 등급 → asset/social/overall 분리),
  프레임에서 "상업=사회" 별칭 제거(`prompts.json:3-4` 재작성), `esg`→`environmental` 명확화.
- **social-primary**: 보고서 렌더가 **사회적 가치를 1급 결론**으로 앞세우고(레포 목적), 자산/상업
  가치는 맥락·이중트랙의 대응 축으로. (`report.render_markdown` 순서·강조 조정.)

---

## 7. 제약(규약) 변경 — 코드로 강제

- **firewall(P1)**: 사회 profile에서 상업 엣지 ≤0.2(음악 기존)·RE 가격/역세권 제외. workstream A
  `edge_prior=semantic`로 자동화(손튜닝 표 의존 제거).
- **additionality 게이트(P2)**: baseline(법정 의무·단순 존재) 식별 → 초과분만 사회 evidence로.
- **tier·absence(P5)**: `evidence_tier`별 신뢰도 가중, age-adjust, **부재→unknown**(신곡/구곡/비서구·
  한국 데이터갭에 0점 페널티 금지).
- **work-vs-artist 일관(음악 모순 해소)**: 곡-내재 사회가치와 아티스트/팬덤-증폭 사회가치를
  **분리 sub-track**으로(붕괴 금지). harvester 사회축 완화(`harvester.py:486`)는 *artist-amplified*
  버킷으로 라벨링하되 work-intrinsic 점수엔 안 섞음.

## 8. 프롬프트 변경
- `config/music_prompts.json`: `base.frame` 사회가치 정의를 §3.1 차원으로 구조화 + tier 명시;
  `music_synthesizer.rules` social_grade 루브릭 차원·tier 기반 재작성; reranker/graph_proposer 사회
  지침을 work/artist 분리에 일관.
- `config/prompts.json`(부동산): "사회적 가치=상업" 별칭 제거, 좁은 S-pillar 정의로 교체, social_grade
  루브릭 신설, esg→environmental.

---

## 9. 단계별 실행계획

- **Phase 1 — 음악 사회가치 재정초 (지금, 현 포커스·레포 목적 직격):**
  ① axes 의도 구조화 + 루브릭(§3.1·§6) → ② HeritageRecognition 노드+RECOGNIZED_BY+속성`kind`
  (§4.1) → ③ 다출처 시딩 C1 레지스트리·C2 longevity(MusicBrainz 기보유)·가사테마(§5.1) → ④ work/artist
  분리(§7) → ⑤ tier·firewall 코드화 → ⑥ 골든 작품 재평가(LOVE ATTACK가 0→유의미 사회근거 갖는지).
- **Phase 2 — 부동산 사회축 신설:** esg→environmental 분리 + social_value 축·profile·노드(§4.2) +
  한국 T1 커넥터(§5.2) + social_grade(§6).
- **Phase 3 — 통합 아키텍처:** 등급 social-primary 렌더·증거 tiering·additionality를 infra 공통
  메커니즘으로 승격(차원은 자산 하위 유지).

## 10. 측정·검증
- 음악 Phase 1: LOVE ATTACK·Dynamite 재평가 — 사회 노드 수(0→N), 차원 커버리지, social_grade가
  상업과 *독립적으로* 움직이는지(firewall 검증), tier 분포.
- 회귀: 기존 81 테스트 + 신규 social 스키마/추출 단위 테스트. 특성화: 양 자산 social_grade 계약.

## 11. 결정 사항 (사용자 확정 2026-07-01)
- **D1 ✅ 확정 — social-primary(단일 주결론)**: 사회적 가치를 보고서의 *주 결론*으로 둔다. 자산/상업
  가치는 대응 맥락(이중트랙의 보조 축). 함의: `synthesizer` social_grade를 1급으로, `report`
  렌더가 사회가치를 앞세움(§6). 기존 코드가 보유한 "무슨 데이터·어느 축" 골격은 살리되 *자산가치
  위주*였던 비중을 사회가치 중심으로 재조정.
- **D2 ✅ 확정 — 소스 추가(내가 조사·구현)**: 한국곡은 한국 사이트(멜론 등)·Spotify 등에서 정보
  취득/크롤링 가능성을 검증. → 별도 분석 [`structured_ingestion.md`](structured_ingestion.md).
- **D3 ⏳ 분석 필요 — 외부 정형데이터의 (a) 사회가치 적합성 (b) 적재 타이밍·방법**: "웹전용 밖"은
  *웹검색과 무관하게 추가되는 정형데이터*를 뜻하며 추가에 동의. 단 **(a) 이 데이터가 사회가치
  보고서에 쓸 만한가, (b) 언제·어떻게 그래프에 적재하는가**는 미고찰 → [`structured_ingestion.md`]
  (structured_ingestion.md)에서 분석. (핵심 긴장: 음악 정형 API 다수가 *상업*(차트·스트리밍)이라
  firewall P1상 사회축엔 부적합 — 사회 적합 정형데이터는 *레지스트리·파생작·사회공헌 수상*으로 좁다.)
- **D4 ✅ 확정 — 음악 Phase 1 완료 후 부동산**.

## 12. 참고문헌 (핵심)
**음악·문화가치**: Throsby, *Economics and Culture*(2001) 6차원 · Holden, *Capturing Cultural Value*
(Demos 2004) intrinsic/instrumental/institutional · AHRC Cultural Value Project(Crossick&Kaszynska 2016,
과대주장 경계) · WHO Fancourt&Finn(2019, HEN67) · LOC National Recording Registry · Grammy Hall of Fame ·
van der Hoeven&Hitters(2019, *Cities*) · Nye soft power · UNESCO Culture|2030.
**부동산 사회가치**: UKGBC *Framework for Defining Social Value*(2021, 추가성) · RICS/Fujiwara&Dass
(2020, SROI 거부·CBA/wellbeing) · UK Social Value Act(2012) · GRESB/EPRA sBPR/GRI 400(좁은 S-pillar) ·
Natural England ANGSt · KoSEA 사회적가치지표(SVI, 이식 가능 루브릭) · KRIHS 10분 생활SOC · SK/CSES DBL.
**공통 경고**: 검증된 단위 사회가치 척도 부재 → 합성지표는 문서화된 설계 선택. SROI 비율 편차
£0.79~28(비교불능) → 비화폐 composite 권장.
