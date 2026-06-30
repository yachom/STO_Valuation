"""Neo4j 지식 그래프 스키마 (음악저작권) — 노드/엣지 타입 + UNIQUE constraint.

real_estate/graphflow/graph/schema.py를 미러링한다. influence(영향력)·
popularity(대중성)·sustainability(지속성) 3축 평가의 evidence가 모이는 그래프.
앵커는 Work(real_estate의 Asset에 해당), 식별자는 placeholder work_id.

스키마 *내용*(라벨/엣지/키)은 도메인이라 여기서 소유하고, 적용 *메커니즘*
(GraphSchemaSpec → CREATE CONSTRAINT)은 infra.graph.schema에 위임한다 — 같은
메커니즘을 두 번째 자산이 그대로 재사용함을 보이는 합성 지점.

⚠️ TODO(domain): 라벨/엣지/키는 명백히 placeholder 집합이다(태스크 지정).
도메인이 확정되면 실제 음악 도메인 엔티티(앨범·트랙·작곡가·세션 등)로 정련한다.

레퍼런스: docs/design/graph_native_flow.md §4
"""

from __future__ import annotations

from neo4j import Driver

from infra.graph.schema import GraphSchemaSpec, _constraint_name


# ───────────────────────────────────────────────────────────
# 노드 라벨 상수 (사용처 typo 방지) — placeholder 집합
# ───────────────────────────────────────────────────────────
class NODE_LABELS:
    # ── 자산 스코프 ──
    WORK = "Work"                # 평가 대상 저작물(앵커) — mc:{work_id}
    RELEASE = "Release"          # 앨범/싱글/EP/리패키지 등 발매 단위 — release_id
    ARTIST = "Artist"            # 아티스트/연주자 — artist_id
    RIGHTSHOLDER = "Rightsholder"  # 권리자(퍼블리셔/레이블) — rightsholder_id
    AGENCY = "Agency"              # 기획사/매니지먼트/레이블 조직 — agency_id
    GENRE = "Genre"                # 장르/스타일 — genre_id
    PLATFORM = "Platform"        # 유통 플랫폼(스트리밍 등) — platform_id
    CHART = "Chart"              # 차트 진입 기록 — chart_id
    AWARD = "Award"              # 수상/노미네이트 — award_id
    MEDIA_EVENT = "MediaEvent"    # 방송·공연·페스티벌·시상식 노출 맥락 — event_id
    VIRAL_TREND = "ViralTrend"    # 챌린지·밈·댄스·해시태그 등 확산 현상 — trend_id
    CERTIFICATION = "Certification"  # 음반 인증(RIAA/Gaon Gold·Platinum) — certification_id
    SOCIAL_METRIC = "SocialMetric"   # 소셜/스트리밍 지표 스냅샷(팔로워·조회·사용수) — metric_id
    FANDOM = "Fandom"                # 공식/비공식 팬덤 공동체 — fandom_id

    # ── 사회적 가치 신호 (가사·메시지·운동·공익 활용) ──
    SOCIAL_THEME = "SocialTheme"     # 작품이 다루는 사회적 의제(정의·인권·정신건강·환경 등) — theme_id
    CAUSE = "Cause"                  # 연계된 사회운동·시위·공익/자선 캠페인 — cause_id
    PUBLIC_USE = "PublicUse"         # 공익·교육·기념·문화외교 등 공적 활용 — use_id

    # ── 확장 수집물 (web-search-first) ──
    WEB_DOC = "WebDoc"           # 웹/뉴스/비평 문서 — url_hash
    FACT = "Fact"                # 원자적 사실 1문장 — fact_id
    ENTITY = "Entity"            # 라벨 매칭 실패 추출 엔티티 — entity_id
    QUERY = "Query"              # 실행한 검색어 — query_hash

    # ── 보고서 grounding (real_estate와 동일 — 보고서가 그래프의 1급 시민) ──
    REPORT = "Report"            # 생성된 보고서 — report_id
    REPORT_SECTION = "ReportSection"  # 축 섹션 — section_id
    CLAIM = "Claim"              # 주장(문장 단위) — claim_id


# ───────────────────────────────────────────────────────────
# 엣지 타입 상수 — placeholder 집합
# ───────────────────────────────────────────────────────────
class EDGE_TYPES:
    # 구조 (Seeder)
    BY_ARTIST = "BY_ARTIST"          # Work → Artist
    PART_OF_RELEASE = "PART_OF_RELEASE"  # Work → Release
    HELD_BY = "HELD_BY"              # Work → Rightsholder
    MANAGED_BY = "MANAGED_BY"        # Artist → Agency
    HAS_FANDOM = "HAS_FANDOM"        # Artist → Fandom
    HAS_GENRE = "HAS_GENRE"          # Work → Genre
    DISTRIBUTED_ON = "DISTRIBUTED_ON"  # Work → Platform
    CHARTED_ON = "CHARTED_ON"        # Work → Chart
    WON = "WON"                      # Work|Artist → Award
    FEATURED_IN = "FEATURED_IN"      # Work|Artist → MediaEvent
    SPREAD_AS = "SPREAD_AS"          # Work → ViralTrend
    CERTIFIED = "CERTIFIED"          # Work → Certification
    HAS_METRIC = "HAS_METRIC"        # Work|Platform → SocialMetric
    COVERED_BY = "COVERED_BY"        # Work → Artist (이 곡을 커버한 아티스트 — 영향력)
    SAMPLED_BY = "SAMPLED_BY"        # Work → Artist|Work (이 곡을 샘플링한 작품 — 영향력)
    ADDRESSES = "ADDRESSES"          # Work → SocialTheme (이 곡이 다루는 사회적 의제 — 사회적 가치)
    SUPPORTED = "SUPPORTED"          # Work → Cause (이 곡이 연대·후원한 사회운동/캠페인 — 사회적 가치)
    USED_IN = "USED_IN"              # Work → PublicUse (공익·교육·기념·외교 활용 — 사회적 가치)

    # 확장 수집 (Harvester/Navigator) — real_estate와 동일 의미
    SPAWNED_QUERY = "SPAWNED_QUERY"  # 노드 일반 → Query
    RETRIEVED = "RETRIEVED"          # Query → WebDoc
    EXTRACTED_FROM = "EXTRACTED_FROM"  # Fact → WebDoc
    EXTRACTED_BY = "EXTRACTED_BY"      # Fact → Query (조사 과제/축 provenance)
    MENTIONS = "MENTIONS"            # Fact → 노드 일반 (확정 연결)
    SAME_AS_CANDIDATE = "SAME_AS_CANDIDATE"  # Entity → 노드 일반 (미확정)
    ABOUT = "ABOUT"                  # WebDoc|Report → Entity|Work|노드 일반

    # 보고서 grounding
    HAS_SECTION = "HAS_SECTION"      # Report → ReportSection
    HAS_CLAIM = "HAS_CLAIM"          # ReportSection → Claim
    CITES = "CITES"                  # Claim → 노드 일반 (증거)


# ───────────────────────────────────────────────────────────
# 엣지 엔드포인트 — 각 value/구조 엣지가 잇는 (src 라벨, dst 라벨). 위 EDGE_TYPES 방향
# 주석의 코드화이며 **단일 출처**다. agentic_graph validator가 LLM이 제안한 관계의 스키마
# 합법성(예: CHARTED_ON은 Work→Chart만)을 검증하는 데 쓴다. provenance(MENTIONS/
# EXTRACTED_*/SPAWNED_QUERY/RETRIEVED/ABOUT/SAME_AS_CANDIDATE)·보고서(HAS_SECTION/
# HAS_CLAIM/CITES) 엣지는 LLM 제안 대상이 아니라 제외한다.
# ───────────────────────────────────────────────────────────
_L = NODE_LABELS
_E = EDGE_TYPES
EDGE_ENDPOINTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    _E.BY_ARTIST:       (frozenset({_L.WORK}),              frozenset({_L.ARTIST})),
    _E.PART_OF_RELEASE: (frozenset({_L.WORK}),              frozenset({_L.RELEASE})),
    _E.HELD_BY:         (frozenset({_L.WORK}),              frozenset({_L.RIGHTSHOLDER})),
    _E.MANAGED_BY:      (frozenset({_L.ARTIST}),            frozenset({_L.AGENCY})),
    _E.HAS_FANDOM:      (frozenset({_L.ARTIST}),            frozenset({_L.FANDOM})),
    _E.HAS_GENRE:       (frozenset({_L.WORK}),              frozenset({_L.GENRE})),
    _E.DISTRIBUTED_ON:  (frozenset({_L.WORK}),              frozenset({_L.PLATFORM})),
    _E.CHARTED_ON:      (frozenset({_L.WORK}),              frozenset({_L.CHART})),
    _E.WON:             (frozenset({_L.WORK, _L.ARTIST}),   frozenset({_L.AWARD})),
    _E.FEATURED_IN:     (frozenset({_L.WORK, _L.ARTIST}),   frozenset({_L.MEDIA_EVENT})),
    _E.SPREAD_AS:       (frozenset({_L.WORK}),              frozenset({_L.VIRAL_TREND})),
    _E.CERTIFIED:       (frozenset({_L.WORK}),              frozenset({_L.CERTIFICATION})),
    _E.HAS_METRIC:      (frozenset({_L.WORK, _L.PLATFORM}), frozenset({_L.SOCIAL_METRIC})),
    _E.COVERED_BY:      (frozenset({_L.WORK}),              frozenset({_L.ARTIST})),
    _E.SAMPLED_BY:      (frozenset({_L.WORK}),              frozenset({_L.ARTIST, _L.WORK})),
    _E.ADDRESSES:       (frozenset({_L.WORK}),              frozenset({_L.SOCIAL_THEME})),
    _E.SUPPORTED:       (frozenset({_L.WORK}),              frozenset({_L.CAUSE})),
    _E.USED_IN:         (frozenset({_L.WORK}),              frozenset({_L.PUBLIC_USE})),
}


# ───────────────────────────────────────────────────────────
# UNIQUE constraint — 자연 키 보장 (라벨, 키 프로퍼티). idempotent 적재 전제.
# ───────────────────────────────────────────────────────────
_UNIQUE_KEYS: list[tuple[str, str]] = [
    (NODE_LABELS.WORK, "work_id"),
    (NODE_LABELS.RELEASE, "release_id"),
    (NODE_LABELS.ARTIST, "artist_id"),
    (NODE_LABELS.RIGHTSHOLDER, "rightsholder_id"),
    (NODE_LABELS.AGENCY, "agency_id"),
    (NODE_LABELS.GENRE, "genre_id"),
    (NODE_LABELS.PLATFORM, "platform_id"),
    (NODE_LABELS.CHART, "chart_id"),
    (NODE_LABELS.AWARD, "award_id"),
    (NODE_LABELS.MEDIA_EVENT, "event_id"),
    (NODE_LABELS.VIRAL_TREND, "trend_id"),
    (NODE_LABELS.CERTIFICATION, "certification_id"),
    (NODE_LABELS.SOCIAL_METRIC, "metric_id"),
    (NODE_LABELS.FANDOM, "fandom_id"),
    (NODE_LABELS.SOCIAL_THEME, "theme_id"),
    (NODE_LABELS.CAUSE, "cause_id"),
    (NODE_LABELS.PUBLIC_USE, "use_id"),
    (NODE_LABELS.WEB_DOC, "url_hash"),
    (NODE_LABELS.FACT, "fact_id"),
    (NODE_LABELS.ENTITY, "entity_id"),
    (NODE_LABELS.QUERY, "query_hash"),
    (NODE_LABELS.REPORT, "report_id"),
    (NODE_LABELS.REPORT_SECTION, "section_id"),
    (NODE_LABELS.CLAIM, "claim_id"),
]


# 라벨 → 키 프로퍼티 매핑 (제네릭 writer가 MERGE 키로 사용 / walk가 자연키로 주입)
KEY_PROP: dict[str, str] = {label: key for label, key in _UNIQUE_KEYS}


CONSTRAINTS: list[str] = [
    f"CREATE CONSTRAINT {_constraint_name(label, key)} IF NOT EXISTS "
    f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE"
    for label, key in _UNIQUE_KEYS
]


# 음악저작권 자산의 그래프 스키마 명세 — infra 메커니즘에 주입한다.
# node_labels는 NODE_LABELS 클래스의 모든 라벨 값(UPPER 상수)을 모은다.
MUSIC_SCHEMA = GraphSchemaSpec(
    node_labels=tuple(
        v for k, v in vars(NODE_LABELS).items()
        if not k.startswith("_") and isinstance(v, str)
    ),
    edge_types=tuple(
        v for k, v in vars(EDGE_TYPES).items()
        if not k.startswith("_") and isinstance(v, str)
    ),
    key_props=KEY_PROP,
)


def apply_constraints(driver: Driver) -> None:
    """모든 UNIQUE constraint 생성. idempotent. 도메인 spec을 infra 메커니즘에
    주입해 위임한다(real_estate와 동일 패턴 — infra 코드 복제 없음)."""
    from infra.graph.schema import apply_constraints as _ac

    return _ac(driver, MUSIC_SCHEMA)
