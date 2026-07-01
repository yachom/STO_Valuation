# 외부 정형데이터 수집·적재 — 사회가치 관점 분석 (D2 · D3)

> 작성일: 2026-07-01 · 대상: `music_copyright`(우선) · 배경: 사회가치 재정초([`social_value.md`]
> (social_value.md))에서 외부 정형데이터를 (a) 사회가치 보고서에 **쓸 만한가**, (b) **언제·어떻게**
> 그래프에 적재할지 미고찰 → 본 문서가 분석. 적재 *원칙*은 [`agentic_traversal.md §6`]
> (agentic_traversal.md)(혼합 출처: 수집≠적재≠탐색)을 따른다.

---

## 0. 핵심 반전 (먼저 읽기)

직관과 달리 — **음악 외부 정형 API의 다수는 *상업* 신호(차트·스트리밍·popularity)라, 사회가치
firewall(P1)상 사회축에서 제외된다.** 따라서:

1. 멜론·스포티파이에서 "정보를 받아오는" 대부분은 **상업 데이터 → 자산축**이지 사회 보고서가 아니다.
2. **사회가치에 쓸 정형데이터는 좁고, 그중 핵심은 이미 통합돼 있다** — MusicBrainz(파생작=C2),
   Wikidata(수상·사회연계=C1). 새 스크레이퍼를 안 짜도 된다.
3. 진짜 추가 가치는 **한국 서술형 소스(나무위키·한국 위키)와 사회/비평 인정 레지스트리** — 정형
   API가 아니라 *웹 fetch*로 들어오는 것들이다.
4. **전문 가사(full lyrics)는 사회가치에 불필요**하고 법적으로도 제약(아래 §2).

→ 결론: 멜론 차트 스크레이퍼·전문 가사 크롤러를 새로 만드는 것은 *사회축엔* 비용 대비 이득이
낮고 위험하다. 사회 Phase 1은 **기보유 정형(MusicBrainz/Wikidata) 활용 + 한국 서술형 소스 추가**로.

---

## 1. 소스 feasibility 매트릭스 (D2)

판정: ◎ 핵심 사회근거 · ○ 보조 사회근거 · △ 조건부/회색 · ✗ 상업·부적합(자산축)

| 소스 | 제공 | 접근 / 라이선스 | 신호 | 사회가치 판정 |
|---|---|---|---|---|
| **Spotify Web API** | 메타데이터·popularity(0~100)·ID | OAuth 무료. **가사 없음**(앱 내 가사는 Musixmatch). 오디오특성 신규앱 제한(2024) | 상업(popularity) | **△** 식별만(resolver 기보유), 사회신호 아님 |
| **Melon/Genie/FLO/Bugs** | 차트·가사 | **공식 공개 API 없음**(비공식 스크레이퍼=ToS 위험·취약). 가사=KOMCA 라이선스 | 상업(차트) | **✗** 사회 부적합(firewall) + 법·운영 위험 |
| **Musixmatch** | 가사(전문·싱크) | API. 무료=30% 프리뷰·비상업만. **전문/상업=유료 라이선스** | 콘텐츠 | **△** 전문가사 원하면 *유료만 합법* |
| **Genius** | About·주석·관계·pageviews | API(기보유). 전문가사는 웹페이지(ToS 회색) | 서술 + 상업(pageviews) | **○** About=사회맥락(기보유), 전문가사 크롤 금지 |
| **MusicBrainz** | work/recording·**파생작(커버/샘플)**·ISWC·별칭 | 오픈 API(데이터 CC0). **기보유**(`seeder._collect_structured`) | 구조·파생 | **◎** C2 longevity(decade-span)—기보유 활용 |
| **Wikidata** | **수상(P166)**·사회운동/주제 연계·식별자 | 오픈(CC0). **기보유** | 구조·인정 | **◎** C1·S1·S2—기보유 강화 |
| **한국 위키 / Wikipedia / 뉴스** | 서술형 문화·사회 맥락, **가사 *테마* 설명**, 운동 연계 | 웹 fetch. 위키=**CC BY-SA(상업 OK)** | 서술 | **◎** X·S1~S5 (특히 한국곡) |
| ~~나무위키~~ | (서술형 풍부하나) | robots는 `/w/` 허용하나 **콘텐츠=CC BY-NC-SA 2.0 KR(NC)** | 서술 | **✗ 제외**(NC=상업 부적합) |
| **SecondHandSongs** | 커버/샘플 DB(풍부) | API(BETA). **CC BY-NC 3.0 = 비상업·연구만** | 파생 | **△** 상업 제약 → MusicBrainz로 대체 |
| **WhoSampled** | 샘플/커버/리믹스 | 비공식 RapidAPI·독점 데이터 | 파생 | **△** 라이선스 불명 → 보류 |
| **한국대중음악상(KMA)·한국갤럽 등** | 비평·사회적 인정·수상 | 공식 API 없음 → 웹/위키 경유 | 인정 | **○** C1(한국곡), 웹 경유 |
| **Circle Chart(구 Gaon)·MMA·MAMA** | 차트·시상 | 일부 웹 | 상업 | **✗** 상업(firewall) → 자산축 |

---

## 2. 가사(lyrics) 특별 분석

- **전문 가사는 저작권(KOMCA)·ToS로 보호** — 멜론/지니/Genius 페이지 크롤링은 침해·ToS 위반 위험.
  합법 경로는 **Musixmatch 유료 라이선스**뿐(무료는 30%·비상업).
- **그러나 사회가치엔 전문 가사가 *불필요*하다.** 우리가 필요한 건 가사의 *사회적 테마*(X 차원)와
  맥락이고, 그건 **서술형 2차 소스**(나무위키·위키·Genius About·뉴스/비평)에서 합법적으로 얻는다 —
  이미 web harvest + Genius About 경로가 있다. 가사 전문은 grounding 발췌로도 굳이 필요 없다.
- **권고**: 전문 가사 크롤링/스크레이핑은 하지 않는다. (전문 가사가 꼭 필요해지면 그때 Musixmatch
  라이선스를 별도 검토 — D 결정.)

---

## 3. 사회가치 적합성 — firewall이 거른다 (D3-a)

P1(상업 firewall)을 적용하면 외부 정형데이터가 셋으로 갈린다:

- **상업 → 자산축(사회 제외)**: Spotify popularity, 멜론/Circle 차트, 스트리밍 카운트, 인증(판매).
  *사용자가 언급한 "실시간 차트"가 여기* — 가치 있으나 **자산축 전용**, 사회 보고서엔 안 들어간다.
- **사회 적합(포함)**: ① MusicBrainz 파생작 → **C2 세대전이 longevity** ② Wikidata 수상(사회공헌·평화
  등)·사회운동/주제 연계 → **C1·S1·S2** ③ 나무위키/위키/뉴스 서술 → **X·S1~S5** ④ 사회/비평 인정
  레지스트리(KMA 등) → **C1**.
- **회색(보류)**: SecondHandSongs(CC BY-NC=상업 제약), WhoSampled(독점·불명), 전문 가사(라이선스).

> 즉 "어떤 외부 데이터를 사회가치에 쓰나"의 답은 **상업 지표가 아니라 *인정·파생·서술형 맥락***이다.
> 그리고 그 핵심(MusicBrainz·Wikidata)은 이미 시더에 있다 — 사회축 용도로 *재사용·강화*가 핵심.

---

## 4. 적재 타이밍·방법 (D3-b)

균일 Loader 계약(typed node + Fact + provenance 엣지 + `source_tier` + `evidence_tier`)을 따르되,
데이터의 **휘발성·신뢰도**에 따라 타이밍이 갈린다:

| 데이터 유형 | 예시 | 타이밍 | 적재 노드/엣지 | tier |
|---|---|---|---|---|
| **안정 구조·인정** | MusicBrainz 파생작, Wikidata 수상, KMA 인정 | **seed-time(앵커)**, 드물게 refresh | HeritageRecognition·`RECOGNIZED_BY` / `COVERED_BY`·`SAMPLED_BY`(+연도) / `WON` | source_tier≈1, **T1** |
| **서술형 사회맥락** | 나무위키·위키·Genius About·뉴스 | **harvest-time(웹)**, grounding 계약(verbatim 발췌) | SocialTheme·Cause·PublicUse(+`kind`) ← grounded Fact | 출처별 tier, **T2** |
| **휘발성 상업** | 실시간 차트·스트리밍(멜론/Spotify) | **seed snapshot + validity/staleness**(`agentic_traversal.md §6.3`) | Chart·SocialMetric → **자산축 전용** | source_tier≈1, T1(단 firewall로 사회 제외) |

핵심 규칙:
- **안정 구조·인정 데이터가 먼저(seed-time)** 앵커로 들어가야 web harvest가 거기에 붙는다(순서).
  이들은 거의 안 변하므로 staleness 갱신은 드물게.
- **서술형은 web harvest 경로**(기존 navigator→harvester)로 들어오되, 사회 차원별 질의를 강화
  (`navigator._social_query_seeds`를 운동·기부·교육·기념·대표성으로). grounding 계약 유지.
- **휘발성 상업(실시간 차트)**은 valid_until 스냅샷 + 매 run staleness-gated refresh로 적재하되,
  **자산축으로만** 라우팅(firewall). 사회 보고서엔 영향 없음.
- 모든 적재물에 `evidence_tier`(T1/T2/T3) 부여 → 등급 신뢰도·absence≠zero(P5)에 사용.

---

## 5. 권고 (사회 Phase 1)

**추가/강화할 것:**
1. **MusicBrainz 파생작 → C2 longevity**(기보유 데이터 재사용): 커버/샘플의 *연도 span*을 사회/지속
   점수로. 신규 커넥터 0.
2. **Wikidata 수상·사회연계 → C1·S**(기보유 강화): 사회공헌·평화·문화 수상, 사회운동/주제 연계 추출.
3. **한국 서술형 커넥터(신규, web fetch)**: **한국 위키(CC BY-SA)·뉴스** → 사회 테마·맥락(특히
   한국곡). (나무위키는 CC BY-NC-SA의 NC로 상업 부적합 → 제외.) Genius About가 비는 한국곡의
   0-노드 문제 직접 해소.
4. **사회/비평 인정(C1) — KMA 등**: 웹/위키 경유로 HeritageRecognition 적재.

**추가하지 않을 것(또는 자산축으로):**
- 멜론/Spotify **차트·스트리밍·popularity** → 상업(firewall) → 자산축. 새 스크레이퍼 사회 목적 불가.
- **전문 가사 크롤링** → 법적 위험. (필요 시 Musixmatch 유료 라이선스 별도 검토.)
- SecondHandSongs(비상업 제약)·WhoSampled(불명) → MusicBrainz로 대체, 보류.

## 6. 확인 필요 (사용자/후속)
- **나무위키 = 제외 확정(2026-07-01)**: robots.txt는 `/w/`를 허용하나 콘텐츠 라이선스가
  **CC BY-NC-SA 2.0 KR(NC=비상업)**이라 상업 제품에 부적합. → **한국 위키(CC BY-SA)·뉴스**로 대체.
- **전문 가사 필요 여부**: 사회 테마를 서술형으로 충분히 잡으면 불필요. 굳이 전문 가사면 Musixmatch
  유료(별도 결정).
- **실시간 차트(자산축)**: 본 사회 작업과 분리된 트랙 — 자산가치 보강 시 별도 진행.

## 7. 참고 (소스 확인)
- Spotify Web API(가사 미제공): https://developer.spotify.com/documentation/web-api
- Musixmatch(무료 30%·비상업 / 유료 라이선스): https://freeapihub.com/apis/musixmatch
- Melon 비공식 차트 API·KOMCA: https://github.com/love5757/music-api-kr · https://en.wikipedia.org/wiki/Korea_Music_Copyright_Association
- SecondHandSongs API(CC BY-NC 3.0): https://secondhandsongs.com/page/API
- MusicBrainz·Wikidata: 기존 통합(`music_copyright/external/{musicbrainz,wikidata}`)
