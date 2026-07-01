# 한국 음악(K-pop) 정성평가용 웹서치 소스 설계

> GraphRAG harvester가 곡을 **영향력·대중성/팬덤·지속성·사회공헌·혁신·ESG(연계주체 conduct)**
> 축으로 평가하기 위해 수집해야 하는 것: 뉴스(사건/시상/차트) · 커뮤니티/여론(블로그·카페·팬
> 반응 = 정성 sentiment) · 공식 소스 · 이상적으로 YouTube 댓글/커뮤니티. 이 문서는 각 소스의
> **정확한 rate limit·quota·auth·한국어 커버리지**를 실측/공식문서 기준으로 정리하고 최적
> 채널 조합을 권고한다. (조사일 2026-06-30, 모든 수치는 공식 문서 교차검증)

---

## 0. TL;DR — 결론부터

1. **가장 큰(그리고 가장 싼) 개선은 새 소스가 아니라 이미 짜여 있는 코드를 배선하는 것.**
   NAVER `blog`/`cafearticle`/`kin` vertical 도구(`NaverVerticalSearchTool`,
   `fetch_vertical_metadata`)는 **이미 구현·검증돼 있는데 harvester가 NEWS만 호출**한다. 실측:
   `아이유 좋은날` → blog/cafe/kin 각 3건에 콘서트 팬반응·"결혼행진곡으로 좋은날 괜찮을까요"
   ·"[3단고음 15주년]" 회고·"조용필 vs 아이유 뭐가 더 좋아요" 등 **정확히 필요한 정성/팬덤/
   지속성 신호**가 나온다. → harvester에 vertical(blog+cafe+kin) 배선 = 최우선.
2. **한국어 이벤트/뉴스는 NAVER가 압도.** ddgs news는 Bing/Yahoo 인덱스라 한국어가 얇다(실측
   동일 질의 1건). ddgs text는 Bing 인덱스로 naver/tistory 블로그·YouTube를 일부 건지지만
   깊이가 얕다 → **글로벌/영어 보강용**으로 남기고 한국어 1차는 NAVER.
3. **YouTube 댓글**(사용자가 원한 커뮤니티 정성신호)은 현재 미수집(yt-dlp는 메타데이터만).
   YouTube Data API v3로 저비용 수집 가능하나 **새 API key + 신규 클라이언트** 필요, 그리고
   **2026-06-01부터 quota가 버킷제로 바뀌어** search가 별도 100회/일 버킷으로 분리됐다(후술).
4. **Kakao/Daum**은 NAVER와 겹치지 않는 콘텐츠(Tistory 블로그·Daum 카페)를 잡지만 Daum 점유율
   ~3%라 **보조**. 게다가 2025-12 Daum이 Kakao에서 분사(AXZ/Upstage) → API 약관 변동 리스크.

우선순위: **NAVER(news+blog+cafe+kin)** ≫ ddgs(text/news, 글로벌) ≈ **YouTube Data API(댓글·통계)**
> Kakao(Tistory 보조) > 나머지.

---

## 1. 현재 채널 (harvester가 지금 쓰는 것)

채널 어휘는 `SearchChannel = Literal["web","news","youtube"]`
(`music_copyright/graphflow/research.py:19`), `_ALLOWED_CHANNELS = {"web","news","youtube"}`
(`navigator.py:30`). `QueryVariant.channels` 기본값 `["web","news"]`.

`Harvester._search` (`music_copyright/graphflow/harvester.py` ~L568-585) 매핑:

| 논리 채널 | 실제 호출 | 키 | 비고 |
|---|---|---|---|
| `web` | ddgs **text** (`_ddgs_docs text`) | 무키 | Bing/Brave/Mojeek 등 |
| `news` | ddgs **news** + Tavily(키 있으면) + **NAVER `NaverNewsSearchTool`** | Tavily/NAVER 키 | **NAVER는 news만** |
| (`web`∪`news`) | NAVER 발동 조건 (`"news" in wanted or "web" in wanted`) | | 그런데 호출은 news 엔드포인트뿐 |
| `youtube` | yt-dlp `ytsearchN:` (`YouTubeSearchTool`) | 무키 | **메타데이터만**(제목/설명/조회수/좋아요), **댓글 없음** |

- Tavily는 quota 소진(HTTP 432 / usage limit) 시 self-disable (`_DISABLED_SEARCH_PROVIDERS`).
- `.env` 현재 보유 키: `NAVER_CLIENT_ID/SECRET`, `TAVILY_API_KEY`, **`KAKAO_API_KEY`**(부동산
  Local API용, 검색 API에도 동일 REST키 재사용 가능), `GENIUS_ACCESS_TOKEN`. **`YOUTUBE_API_KEY`
  없음**, `googleapiclient` 미설치(yt-dlp만 설치).

### 현재 갭 (Korean music 관점)

1. **✅ NAVER blog/cafe 배선 완료(2026-07-01)** — harvester가 `NaverVerticalSearchTool`로
   blog·cafearticle을 물림(`_naver_vertical_docs`, channel="community"). 팬 반응·사용례
   ('결혼행진곡으로 좋은날' 등)가 실제 그래프에 유입 확인(naver_blog/naver_cafearticle Fact).
   **kin(지식iN)·webkr은 여전히 미배선**(선택 확장).
2. **YouTube 댓글 미수집** — yt-dlp 경로는 통계/설명만. 팬 sentiment(댓글)를 못 봄.
3. **ddgs news 한국어 취약** — Bing/Yahoo 뉴스 인덱스가 한국어 얇음(실측 1건). NAVER news ≫.
4. **Kakao/Daum 미사용** — Tistory·Daum카페 2차 코퍼스 미활용(키는 이미 보유).
5. **ddgs `videos`** — ⚠️정정(2026-07-01 실측): 결과 **대부분이 YouTube**다(Bing 비디오가 유튜브를
   대량 인덱싱). 댓글은 없지만 **제목에 커버 아티스트가 그대로 노출**(예 "[COVER by 김수영]",
   "Cover by Raon Lee")돼 **커버곡 인식(C2 세대전이 longevity)에 유효** — MB/Genius가 놓치는 유튜브
   롱테일 커버를 잡는다. 미배선(도입 후보). (구 기술 "YouTube 아님·무의미"는 오류였음.)

---

## 2. ddgs 9.12.0 전체 매트릭스 (category × backend × provider)

로컬 레지스트리 introspection(`ddgs.engines.ENGINES`) 결과 — **backend 이름 ≠ 실제 provider**:

| category | enabled backends | 실제 provider (검색원) |
|---|---|---|
| **text** [8] | brave, duckduckgo, google, grokipedia, mojeek, wikipedia, yahoo, yandex | brave=**Brave**(독립) · duckduckgo=**Bing**(html.duckduckgo.com) · google=**Google** · grokipedia=**Grokipedia**(xAI 백과) · mojeek=**Mojeek**(독립 크롤러) · wikipedia=**Wikipedia** opensearch API · yahoo=**Bing**(search.yahoo.com) · yandex=**Yandex** *site-search*(yandex.com/search/site) |
| **news** [3] | bing, duckduckgo, yahoo | bing=**Bing News** · duckduckgo=**Bing**(news.js) · yahoo=**Yahoo News**(독립) |
| **images** [1] | duckduckgo | **Bing** images |
| **videos** [1] | duckduckgo | **Bing 비디오 = YouTube 대량 인덱싱** (실측 전부 youtube.com) — 커버곡 인식에 유효 |
| **books** [1] | annasarchive | **Anna's Archive**(그림자도서관 — ToS 리스크, 음악 무관·제외) |

**핵심**: text에서 duckduckgo·yahoo는 **둘 다 Bing 스크랩(중복)**, brave·mojeek만 독립. news에서
bing·duckduckgo도 **둘 다 Bing(중복)**, yahoo만 별개. **한국 강세 엔진은 없음**(Naver/Daum은
ddgs backend가 아님). Google이 그나마 한국어 최상이나 이 환경에서 ConnectError로 사실상 제외.

레포 기본 backend `"bing, duckduckgo, yahoo, brave, mojeek"`를 카테고리별 유효셋과 교집합
(`DdgsClient._backends_for`, `client.py:90`):
- **text** 교집합 → `{duckduckgo, yahoo, brave, mojeek}` = **2×Bing(중복) + Brave + Mojeek**
  (bing은 text에서 무효라 제거, google은 기본목록에 없음).
- **news** 교집합 → `{bing, duckduckgo, yahoo}` = **2×Bing(중복) + Yahoo**.

### 한국어 음악 커버리지 (실측 `아이유 좋은날 반응 명곡`, region=kr-ko)

- ddgs **text** → 4건: youtube.com · tistory 블로그 · blog.naver.com ×2 → **한국어 OK 수준**
  (Bing 인덱스가 naver/tistory 블로그를 일부 노출). 단 결과 폭이 얕음.
- ddgs **news** → 1건(heraldcorp) → **한국어 취약**. NAVER news로 대체 필요.
- Brave/Mojeek: 독립이나 한국어 depth 약함. Yandex site-search: 범용 아님·저가치.
  Grokipedia/Wikipedia: 백과형 → 아티스트/레이블 팩트 grounding엔 유용, sentiment엔 무용.

**결론**: ddgs는 **글로벌/영어 커버리지 + Bing 경유 한국 블로그 일부**엔 유효하나, 한국어
커뮤니티/뉴스의 NAVER 대체는 불가. `videos` 카테고리는 사용자의 YouTube-댓글 니즈와 무관(별도
yt-dlp / YouTube Data API 경로 유지).

---

## 3. NAVER 검색 오픈 API — 전체 sub-search

- **Auth**: 요청 헤더 `X-Naver-Client-Id` + `X-Naver-Client-Secret` (OAuth 아님).
  developers.naver.com 앱 등록 → 검색 API 신청 → Client ID/Secret. (레포 `NaverCredentials`가 이미
  정확히 이 헤더 주입.)
- **엔드포인트**: `GET https://openapi.naver.com/v1/search/{type}.json`, query UTF-8 인코딩.
  응답 envelope: `lastBuildDate, total, start, display, items[]`.
- **⚠️ Rate limit (공식 확정)**: **검색 API 하루 호출 한도 = 25,000회/일, 앱당, 전 sub-search
  공유 풀**(*"검색 API의 하루 호출 한도는 25,000회입니다."*). **초당 한도도 공식적으로 존재하나
  숫자는 미공개**(흔히 인용되는 ~10 req/s는 secondary/일화). 초과 시 **HTTP 429**(일간·초당 공통).
  → 레포가 `status==429 → NaverRateLimited`로 이미 처리. **body errorCode "010"/"012"는 NAVER
  코드가 아님(오해)** — 실제 quota는 HTTP 429뿐. (참고: `naver_search/client.py:35-41`의
  `_ERROR_CODE_MAP`에 있는 `"010"/"012"→Quota` 매핑은 사문화(死文)이고, `"SE01"→Auth`도 실제로는
  400 잘못된 쿼리(BadRequest)에 가까움 — **정확도 개선 여지, 본 문서 범위 밖**.)
- **에러코드(공식)**: `SE01` 잘못된 쿼리 · `SE02` display 값 · `SE03` start 값(>1000) · `SE04`
  sort 값 · `SE05` 없는 API · `SE06` 인코딩 · `SE99` 시스템 · 게이트웨이 `024`=401 인증실패 ·
  `429`=quota/rate 초과.
- **페이징 상한**: `display` 1–100, `start` 1–1000 → **질의당 최대 ~1,000건**.

| # | 이름 | type / URL(`…/v1/search/`) | 내용·핵심 필드 | display | start | sort |
|---|---|---|---|:--:|:--:|---|
| 1 | 블로그 blog | `blog.json` | 네이버 블로그 — title, link, description, **bloggername**, postdate | 100 | 1000 | sim(기), date |
| 2 | 뉴스 news | `news.json` | 뉴스 — title, originallink, link, description, pubDate | 100 | 1000 | sim(기), date |
| 3 | 카페글 cafearticle | `cafearticle.json` | 네이버 카페 — title, link, description, **cafename** | 100 | 1000 | sim(기), date |
| 4 | 웹문서 webkr | `webkr.json` | 웹문서 — title, link, description | 100 | 1000 | **없음** |
| 5 | 지식iN kin | `kin.json` | Q&A — title, link, description | 100 | 1000 | sim(기), date, **point** |
| 6 | 전문자료 doc | `doc.json` | 학술/전문자료 | 100 | 1000 | **없음** |
| 7 | 백과사전 encyc | `encyc.json` | 백과 — +thumbnail | 100 | 1000 | (sim) |
| 8 | 책 book | `book.json` | 도서 — author, publisher, isbn, pubdate | 100 | 1000 | sim(기), date |
| 8b | 책상세 book_adv | `book_adv.xml`(**XML전용**) | d_titl/d_isbn 질의 | 100 | 1000 | sim, date |
| 9 | 이미지 image | `image`(**.json 미표기**) | 이미지 — thumbnail, size | 100 | 1000 | sim, date + filter |
| 10 | 지역 local | `local.json` | 업체/장소 — address, mapx/y | **5** | **1** | random(기), comment |
| 11 | 쇼핑 shop | `shop.json` | 상품 — lprice, mallName, brand, category1–4 | 100 | 1000 | sim, date, asc, dsc |
| 12 | 영화 movie | `movie.json` | **2023-03-31 지원 종료(공식)** | — | — | — |
| — | 음악/music | — | **존재한 적 없음**(VIBE는 별도 B2B GraphQL) | — | — | — |
| util | errata / adult | — | 오타변환 / 성인 판별(0·1) | — | — | — |

> **주의**: `local`만 예외 — display 5·start 1(트래픽 사유, 페이징 불가). `webkr`·`doc`은 sort
> 없음. `kin`만 `sort=point`(추천수) 추가.

### K-pop 곡/아티스트 평가용 sub-search 랭킹

- **Tier 1 (핵심 신호)** — **cafearticle**(네이버 카페 = 한국 팬덤 본진, 공식 팬카페·최고 밀도
  팬 sentiment; 다만 가입제 카페 상당수는 공개글만 API 노출) · **blog**(공개 후기/리뷰 = 대중
  반응 폭) · **news**(차트1위·초동/판매·시상·컴백 = 이벤트 + **리스크(논란) 채널**).
- **Tier 2 (인지/그라운딩/상업 proxy)** — **kin**(질문 = 기존 팬 밖 호기심·도달의 선행지표,
  affinity 아님 reach) · **shop**(앨범/포카/굿즈 = 상업 traction proxy, STO 자산가치 관련·
  sentiment 아님) · **encyc**(데뷔·멤버·디스코 팩트 grounding).
- **Tier 3 (음악 정성엔 ~무용)** — webkr(잡음) · doc(학술, 메가급 예외만) · book(전기/악보) ·
  image(존재만) · local(공연장/팝업 한정). movie=사망, music=없음.
- 한 줄: **cafearticle+blog = 팬 sentiment · news = 이벤트/리스크 · kin = 인지 선행 · shop =
  상업 proxy · encyc = 팩트**. 나머지 ~0.

---

## 4. Kakao(Daum) 검색 REST API

- **Auth**: 헤더 `Authorization: KakaoAK ${REST_API_KEY}` (레포 `KAKAO_API_KEY` 재사용 가능 —
  부동산은 같은 host의 **Local API** `/v2/local/search/*`를 이미 사용 중).
- **상태(2026 라이브 문서)**: 6개 콘텐츠 검색 엔드포인트 **모두 REST 키만으로 개방·비검수·
  비즈니스앱 불요·미deprecated**. 유료 API 목록에도 없음.
- **⚠️ Quota**: **50,000회/일 합산 + 30,000회/일 엔드포인트별**, 3,000,000회/월, **앱당**.
  ("일간 쿼터는 추후 변경될 수 있습니다" 단서). **공식 초당 한도 없음**(간헐 429 보고, 수치
  미공개). 증설은 협의/제휴만.
- **"100k→축소" 루머는 오해** — 그건 같은 `dapi.kakao.com` host의 **Kakao Map/Local API**
  (place search) 유료화(2024-12 공지·2026-07-21 free-quota 정책)이고, **콘텐츠 검색은 무관**.
  (레포 부동산이 쓰는 게 바로 그 Local API이니 혼동 주의.)

| 엔드포인트 | URL(GET) | size 최대(기본) | page 최대 | sort(기본) | documents[] |
|---|---|:--:|:--:|---|---|
| 웹문서 web | `v2/search/web` | 50(10) | 50 | accuracy·recency | title, contents, url, datetime |
| 블로그 blog | `v2/search/blog` | 50(10) | 50 | accuracy·recency | +blogname, thumbnail |
| 카페 cafe | `v2/search/cafe` | 50(10) | 50 | accuracy·recency | +cafename, thumbnail |
| 동영상 vclip | `v2/search/vclip` | **30(15)** | **15** | accuracy·recency | +play_time, author, thumbnail |
| 이미지 image | `v2/search/image` | **80(80)** | 50 | accuracy·recency | image_url, display_sitename, doc_url |
| 책 book | **`v3/search/book`** | 50(10) | 50 | accuracy·**latest** | isbn, authors[], publisher, price, status |

- 공통 envelope: `meta{total_count, pageable_count, is_end}` + `documents[]`. `book`만 v3 +
  `target`(title/isbn/publisher/person).

### K-pop 커버리지 vs NAVER — 보조(비중복이나 modest)

- **비중복 이유**: Kakao cafe = **Daum 카페 전용**, Kakao blog = **Tistory**(카카오 소유) — Naver
  API로는 **절대** 못 얻는 property-locked 콘텐츠.
- **최대 가치**: **Tistory 장문 K-pop 콘텐츠**(리뷰/분석/번역) — Naver 블로그 API가 구조적으로
  못 닿음. Daum 카페엔 일부 공식 팬카페 long-tail(예 세븐틴 공식카페) 잔존하나 감소세·가입제 多.
- **한계**: Daum 검색점유 ~3%(vs Naver 58%+) → long-tail 보강. 그리고 **양 포털 모두 진짜 버즈
  (X/Twitter·Instagram·YouTube·Weverse·DC·theqoo·Pann 등)를 못 봄** — 이건 별도 소셜 커넥터 영역.
- **리스크 플래그**: 2025-12 **Daum이 Kakao에서 분사 → 신설 "AXZ"(Upstage 이관)**. Developers
  Daum Search 표면·quota·약관 변동 가능성 非소. 도입 시 external-client/audit seam 뒤로 격리.

---

## 5. YouTube Data API v3 (댓글·통계 — 사용자의 커뮤니티 니즈)

- **⚠️ 2026-06-01 quota 버킷제 전환**(공식 revision history). 이전 "search=100 units" 모델은
  **폐기** — 3개 독립 버킷:

| 버킷 | 한도/일 | 포함 |
|---|---|---|
| **Search Queries** | **search.list 100회/일** | search.list (버킷 내 1 unit) |
| **General** | **10,000 units/일** | videos.list · commentThreads.list · comments.list · channels.list · activities.list … (각 1 unit) |
| **Video Uploads** | insert 100회/일 | videos.insert (무관) |

리셋: 매일 자정 **PT**. 증설: quota extension form(수동 심사).

- **Auth**: **공개 읽기(search·공개 영상 통계·공개 댓글)는 API 키만으로 충분**. OAuth는 댓글
  작성/모더레이션·비공개 데이터에만. → **댓글 하베스팅은 API 키면 됨.**
- **Cost 표(공식 교차검증)**:

| method | cost | 버킷 | 반환 | maxResults |
|---|:--:|---|---|:--:|
| search.list | 1* | Search(100/일) | 영상/채널 ID | 50 |
| videos.list | 1 | General | `statistics`= **viewCount, likeCount, commentCount** | 50 ID/콜 |
| commentThreads.list | 1 | General | 최상위 댓글(+일부 답글) | **100** |
| comments.list | 1 | General | 특정 댓글의 답글(parentId) | 100 |
| channels.list / activities.list | 1 | General | 채널 통계 / 활동피드 | 50 |

  *search.list는 값 1이지만 **별도 100회/일 버킷**이 실질 병목.

- **댓글 상세**: commentThreads.list 콜당 최상위 100개, `order=time`(기본)·`relevance`(engagement
  가중, "top" sentiment에 유리). 전체 답글은 comments.list(parentId). 필드: `textOriginal`(원문),
  `likeCount`, `authorDisplayName`, `publishedAt`. **한국어 원문 그대로**(서버 번역 없음). 댓글
  비활성 영상 → **403 `commentsDisabled`**(공식 MV·자동 "- Topic" 영상에 흔함).
- **커뮤니티 탭 게시물**: **API v3에 없음**(activities.list는 업로드 피드일 뿐). 수집 불가.
- **Quota 예산(곡 50개/일: 각 search 1 + videos.list 1 + commentThreads 1)**:
  - Search 버킷 50/100 (50%) · General 100/10,000 (**1%**) → **여유 대량**.
  - 심화(곡당 댓글 500개=5페이지): General 300/10,000(3%) → 수백 곡/일 가능. videos.list는 50 ID
    batch=1 unit. **실질 상한은 Search 100회/일** → 곡당 1검색이면 ~100곡/일(캐싱/ID재사용으로 완화).
- **Caveats**: (1) K-pop 댓글 = 스트리밍파티·이모지·다국어 스팸 多, 외부 하베스터는 `published`
  댓글만 봄(모더레이션 상태는 OAuth 소유자만) → de-dup·언어판별·번역 필요. (2) 비활성/자동Topic
  영상 구분. (3) 대형 스레드 완전수집 미보장. (4) 소진 시 403 `quotaExceeded`, 버스트시
  `rateLimitExceeded` → 지수 backoff. (5) **ToS(중요)**: Developer Policies상 **저장 API 데이터는
  30일 내 갱신 또는 삭제** 의무 + 원문 댓글·author는 PII → **원문 장기 warehouse 금지, 파생
  sentiment 점수만 보존하고 ≤30일 주기 재fetch** 파이프라인 권장.

---

## 6. 기타 소스 (간단)

- **Genius** (`GENIUS_ACCESS_TOKEN` 보유·시드 커넥터) — 가사/annotation·About = **혁신(작법/가사)·
  사회공헌(가사 주제, social_value 축 연동)** grounding. 검색 하베스팅보다 seed-time 정형수집 역할.
- **Melon/Bugs/Genie** — 차트 권위 최고이나 **공개 API 없음 + 스크래핑 ToS 위반 리스크** → 하베스터
  직접 크롤 지양. 차트 팩트는 Wikidata/MusicBrainz/Soundcharts 등 정형 커넥터 경로로.
- **일반 Google** — 이 환경 ConnectError(사실상 제외). ddgs `google` backend도 동일 이유로 기본
  목록 제외 상태.

---

## 7. 마스터 비교표

| 소스 / sub-search | 콘텐츠 타입 | 한국음악 커버리지 | Auth/키 | Rate limit (일 / 초) | 콜당 cost | best-for 축 |
|---|---|---|---|---|---|---|
| **ddgs text** (Bing·Brave·Mojeek) | 웹 스니펫 | 中(Bing 경유 naver/tistory 일부) | 무키 | 비공식(라이브 스크랩·게이트웨이 완충) | 무료 | 영향력·혁신(글로벌/영어), 크로스마켓 |
| **ddgs news** (Bing·Yahoo) | 뉴스 스니펫 | 弱(실측 1건) | 무키 | 상동 | 무료 | 영향력(글로벌 이벤트) — 한국은 NAVER로 |
| **ddgs videos** | **YouTube**(Bing 비디오) | **커버곡(강)** | 무키 | — | 무료 | **커버 인식(C2 longevity)** — 미배선·도입후보 |
| ddgs images/books | 이미지/그림자도서 | 무관 | 무키 | — | 무료 | (미배선·저가치) |
| **NAVER news** | 한국 뉴스 | **强** | Client-Id/Secret | **25,000/일**(전 sub 공유) / 초당(미공개) | 무료 | 영향력·ESG(논란)·지속성(컴백사) |
| **NAVER blog** | 네이버 블로그 | **强** | 〃 | 25k 공유 | 무료 | **대중성/팬덤**·지속성(회고) |
| **NAVER cafearticle** | 네이버 카페 | **强(팬덤 본진)** | 〃 | 25k 공유 | 무료 | **대중성/팬덤(핵심)**·사회공헌·지속성 |
| **NAVER kin** | 지식iN Q&A | 中 | 〃 | 25k 공유 | 무료 | 대중성(인지·도달 선행) |
| NAVER shop | 상품(앨범/굿즈) | 中 | 〃 | 25k 공유 | 무료 | 대중성(상업 traction proxy) |
| NAVER encyc | 백과 | 中 | 〃 | 25k 공유 | 무료 | 전 축 팩트 grounding |
| NAVER webkr/doc/book/image/local | 웹/학술/책/이미지/지역 | 弱 | 〃 | 25k 공유 | 무료 | (음악 저가치) |
| **Kakao blog** (Tistory) | Tistory 블로그 | 中(비중복 장문) | KakaoAK REST키 | **50,000/일 합산·30,000/일 엔드포인트** | 무료 | 대중성/팬덤·혁신(작법분석)·지속성 |
| **Kakao cafe** (Daum) | Daum 카페 | 弱~中(long-tail) | 〃 | 50k/30k | 무료 | 대중성/팬덤(보조) |
| Kakao web/vclip/image/book | 웹/동영상/이미지/책 | 弱~中 | 〃 | 50k/30k | 무료 | 영향력(영상 존재)·grounding |
| **YouTube search.list** | 영상 ID | 强 | **API 키**(공개 읽기) | **100회/일**(Search 버킷) | 1u | 대중성(도달) |
| **YouTube videos.list** | 조회/좋아요/댓글수 | 强 | 〃 | 10,000u/일(General) | 1u(50 ID batch) | **대중성(하드 engagement)** |
| **YouTube commentThreads** | 댓글 원문·likeCount | 强(한국어 원문) | 〃 | 10,000u/일(General) | 1u/100댓글 | **대중성/팬덤(sentiment)**·사회공헌·혁신(신선도 인식) |
| YouTube(현행 yt-dlp) | 영상 메타데이터 | 强 | 무키 | 라이브 추출 | 무료 | 대중성(조회/좋아요) — **댓글 없음** |
| Genius (seed) | 가사/annotation | 强(영문 위주) | Access Token | — | — | 혁신(작법)·사회공헌(가사주제) |
| Melon/Bugs | 차트 | 强(권위) | **API 없음** | — | — | (ToS 리스크·정형 커넥터로 우회) |

---

## 8. 권고 — 최적 채널 조합 (우선순위)

### P0. NAVER vertical(blog+cafearticle+kin)을 harvester에 배선 — **신규 클라이언트 불요**
`NaverVerticalSearchTool`/`fetch_vertical_metadata`는 이미 존재·검증(§0 실측). 남은 건 배선뿐.
- 최소 변경안: `harvester.__init__`에서 `NaverVerticalSearchTool`도 인스턴스화하고, `_search`의
  NAVER 분기(현재 news만)를 확장해 **blog·cafearticle·kin**을 병렬 태스크로 추가
  (`_naver_docs`와 동형의 `_naver_vertical_docs`). 채널 라우팅 2안:
  - (a) 간단: 기존 `web`/`news` 채널 wanted 시 vertical도 함께 발동(현행 NAVER 발동조건 재사용).
  - (b) 깔끔: `SearchChannel`에 `"community"` 리터럴 추가(`research.py:19` + `navigator.py:30`
    `_ALLOWED_CHANNELS`) → planner/navigator가 팬덤/사회공헌 축에 `community` 채널을 명시 라우팅.
    → **권장(b)**: 정성 채널이 로그·tracing에서 분리 가시화되고, 축별 채널 의도가 보존됨.
- axis 매핑: **대중성/팬덤·사회공헌·지속성 → cafearticle+blog**, **인지/영향력 선행 → kin**,
  **영향력·ESG(논란)·이벤트 → news**(현행 유지). 정성질의는 `sort=sim`, 최신성 필요 시 `date`.
- 예산: 전 sub-search가 25,000/일 **공유 풀** → 곡당 (news+blog+cafe+kin)=4콜이면 일 ~6,000곡
  이론상 여유. 게이트웨이로 초당(미공개 캡) 완충 유지.

### P1. ddgs text/news = 글로벌/영어 보강으로 유지
한국어 1차를 NAVER로 넘기므로 ddgs는 크로스마켓/영어권 반응·해외 차트·global press 보강 역할.
news는 한국어 약하니 **한국 아티스트엔 NAVER news 우선, ddgs news는 해외 이벤트 백업**. (선택:
독립엔진 신호 다양화를 원하면 backend에 `brave, mojeek`를 명시 유지 — 현재 default가 이미 포함.)

### P2. YouTube Data API v3 — 댓글·통계(팬 sentiment) 신규 도입 (여력 있으면)
- **신규**: `YOUTUBE_API_KEY`(Google Cloud 프로젝트) + 경량 클라이언트. **google-api-python-client
  불요** — 레포 `_http` 패턴으로 REST 직접 호출(naver_search 미러링). BaseExternalClient/audit
  seam 준수.
- 흐름: (곡→) search.list 1콜로 공식 MV videoId → videos.list(statistics: view/like/commentCount,
  50 batch) → commentThreads.list(`order=relevance`, maxResults=100) 1콜로 상위 댓글 100개.
- **병목은 Search 100회/일** → search 결과(videoId)를 그래프에 캐싱해 재검색 회피, 또는 seed-time
  정형수집(MusicBrainz/YouTube ID)에서 videoId를 미리 확보해 search 생략 → 그러면 사실상 General
  10,000u/일만 쓰며 수백 곡 심화 댓글 가능.
- **ToS 준수**: 원문 댓글 장기저장 금지(30일 refresh/delete) → 파생 sentiment 점수만 그래프에
  적재하고 원문은 휘발. 비활성 댓글(403) graceful skip.

### P3. Kakao(Daum) — Tistory 장문 보조 (선택·저비중)
- **신규 클라이언트**(`infra/external/kakao_search/`, naver_search 미러 + `_http`, `KAKAO_API_KEY`
  재사용). blog(Tistory)·cafe(Daum) 위주, 출력 비중은 낮게.
- 도입 판단: NAVER+YouTube로 정성 커버가 충분하면 **후순위**. Tistory 심층 리뷰가 특별히 필요할
  때만. **AXZ 분사 리스크**로 audit seam 뒤 격리 필수, 실패 시 drop 용이하게.

### 요약 다이어그램
```
한국어 1차   : NAVER  news(이벤트/리스크) + cafearticle·blog(팬덤 sentiment) + kin(인지) + shop(상업)
글로벌/영어  : ddgs   text(웹) + news(해외 이벤트)   [Bing·Brave·Mojeek, 무키]
커뮤니티/댓글: YouTube Data API  videos.list(engagement) + commentThreads(팬 sentiment 원문)
보조(선택)   : Kakao  blog(Tistory)·cafe(Daum)   / Genius(가사·seed) / 정형커넥터(차트=Melon 우회)
```

---

## 9. 제약·주의 (Constraints & Caveats)

- **NAVER 25,000/일은 앱당 전 sub-search 공유** — vertical 다채널 배선 시 곡당 콜 수(news+blog+
  cafe+kin=4)를 축별로 필요한 것만 라우팅해 예산 관리. 초당 캡(미공개)은 게이트웨이가 완충.
  quota 초과 = HTTP 429(=`NaverRateLimited`). *(코드 노트: `_ERROR_CODE_MAP`의 010/012 quota
  매핑은 실효 없음 — 실제 quota는 429. 정확도 개선은 별도 작업.)*
- **YouTube**: search 100회/일 병목 → videoId 캐싱/seed-time 확보로 우회. 댓글 원문 **30일 저장
  ToS**(Developer Policies) → 파생 점수만 보존. 다국어·스팸 필터 전처리 필수. 403
  commentsDisabled/quotaExceeded/rateLimitExceeded graceful 처리 + backoff.
- **Kakao**: 50k/30k quota는 여유이나 **초당 캡 미공개(간헐 429)**. **Daum→AXZ 분사(2025-12)로
  약관/표면 변동 리스크** → 격리·비중 최소. "100k 축소" 루머는 Local(Map) API 얘기지 검색 아님.
- **ToS 리스크(스크래핑)**: Melon/Bugs/Genie 직접 크롤 금지(공개 API 없음). 네이버/다음 **가입제
  카페 본문**은 API도 공개글만 — 비공개 팬덤 심층은 구조적으로 미도달(과대해석 금지). Anna's
  Archive(ddgs books)는 그림자도서관 → 사용 금지.
- **한국어 depth**: ddgs(Bing) 계열은 한국 커뮤니티 대체 불가 — 정성/팬덤 신호의 진짜 소스는
  NAVER cafe/blog + YouTube 댓글. 진짜 버즈(X/Weverse/DC/theqoo 등)는 어느 검색 API도 안 잡음 →
  향후 별도 소셜 커넥터가 남은 갭.

---

## 10. 출처 (공식 문서)

**NAVER** — `developers.naver.com/docs/serviceapi/search/{blog,news,cafearticle,web,kin,doc,
encyclopedia,book,image,local,shopping,movie}/*.md` · 25,000/일·SE코드·auth 헤더:
`.../search/blog/blog.md` · 429(일간+초당)·`024`: `developers.naver.com/docs/common/openapiguide/
errorcode.md` · 공식 미러 `github.com/naver/naver-openapi-guide`(apilist·Swagger).

**Kakao** — Daum Search dev-guide: `developers.kakao.com/docs/latest/ko/daum-search/dev-guide` ·
quota: `.../ko/getting-started/quota` · 유료 API 목록(Daum 검색 부재): `.../ko/app-setting/paid-api`
· 50k/30k(2021 staff): `devtalk.kakao.com/t/api/119448` · Map/Local 유료화(검색 아님):
`devtalk.kakao.com/t/api-api/140424` · Daum 분사→AXZ: `koreaherald.com/article/10627173`.

**YouTube** — quota/리셋/증설: `developers.google.com/youtube/v3/getting-started` · 비용 계산:
`.../determine_quota_cost` · **2026-06-01 버킷제 전환**: `.../revision_history` · search.list:
`.../docs/search/list` · videos.list: `.../docs/videos/list` · commentThreads.list:
`.../docs/commentThreads/list` · comments: `.../docs/comments` · 에러(quotaExceeded·commentsDisabled):
`.../docs/errors` · 30일 저장 정책: `developers.google.com/youtube/terms/developer-policies`.

**ddgs** — 로컬 레지스트리 introspection(`ddgs.engines.ENGINES`, v9.12.0) + 실측 스모크.

---

## 11. 관련 코드 (파일:라인)

- 채널 어휘: `music_copyright/graphflow/research.py:19` · `navigator.py:30`
- harvester `_search` 매핑: `music_copyright/graphflow/harvester.py` (NAVER news `_naver_docs` +
  **커뮤니티 `_naver_vertical_docs`(blog·cafearticle) 배선됨** · `__init__`에서 두 도구 인스턴스화)
- ddgs 클라이언트/백엔드 교집합: `infra/external/ddgs/client.py:90-97`(`_backends_for`)
- **vertical 도구(배선됨: blog·cafearticle / 미배선: webkr·kin)**: `infra/tool/naver.py`(`NaverVerticalSearchTool`) ·
  `infra/external/naver_search/vertical_search.py`(`fetch_vertical_metadata`, VERTICALS=blog/
  cafearticle/webkr/kin)
- NAVER auth 헤더: `infra/external/naver_search/credentials.py:31`
- NAVER 에러맵(010/012 사문화 노트): `infra/external/naver_search/client.py:35-41`
- YouTube 현행(yt-dlp, 댓글 없음): `infra/external/youtube/client.py` · `infra/tool/youtube.py`
- Kakao REST키 선례(부동산 Local API): `real_estate/database/db_builder/build_master_db.py:110-114`
  (`KakaoAK` 헤더 패턴)
