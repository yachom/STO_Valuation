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
    ARTIST = "Artist"            # 아티스트/연주자 — artist_id
    RIGHTSHOLDER = "Rightsholder"  # 권리자(퍼블리셔/레이블) — rightsholder_id
    PLATFORM = "Platform"        # 유통 플랫폼(스트리밍 등) — platform_id
    CHART = "Chart"              # 차트 진입 기록 — chart_id
    AWARD = "Award"              # 수상/노미네이트 — award_id
    CERTIFICATION = "Certification"  # 음반 인증(RIAA/Gaon Gold·Platinum) — certification_id
    SOCIAL_METRIC = "SocialMetric"   # 소셜/스트리밍 지표 스냅샷(팔로워·조회·사용수) — metric_id

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
    HELD_BY = "HELD_BY"              # Work → Rightsholder
    DISTRIBUTED_ON = "DISTRIBUTED_ON"  # Work → Platform
    CHARTED_ON = "CHARTED_ON"        # Work → Chart
    WON = "WON"                      # Work|Artist → Award
    CERTIFIED = "CERTIFIED"          # Work → Certification
    HAS_METRIC = "HAS_METRIC"        # Work|Platform → SocialMetric
    COVERED_BY = "COVERED_BY"        # Work → Artist (이 곡을 커버한 아티스트 — 영향력)
    SAMPLED_BY = "SAMPLED_BY"        # Work → Artist|Work (이 곡을 샘플링한 작품 — 영향력)

    # 확장 수집 (Harvester/Navigator) — real_estate와 동일 의미
    SPAWNED_QUERY = "SPAWNED_QUERY"  # 노드 일반 → Query
    RETRIEVED = "RETRIEVED"          # Query → WebDoc
    EXTRACTED_FROM = "EXTRACTED_FROM"  # Fact → WebDoc
    MENTIONS = "MENTIONS"            # Fact → 노드 일반 (확정 연결)
    SAME_AS_CANDIDATE = "SAME_AS_CANDIDATE"  # Entity → 노드 일반 (미확정)
    ABOUT = "ABOUT"                  # WebDoc|Report → Entity|Work|노드 일반

    # 보고서 grounding
    HAS_SECTION = "HAS_SECTION"      # Report → ReportSection
    HAS_CLAIM = "HAS_CLAIM"          # ReportSection → Claim
    CITES = "CITES"                  # Claim → 노드 일반 (증거)


# ───────────────────────────────────────────────────────────
# UNIQUE constraint — 자연 키 보장 (라벨, 키 프로퍼티). idempotent 적재 전제.
# ───────────────────────────────────────────────────────────
_UNIQUE_KEYS: list[tuple[str, str]] = [
    (NODE_LABELS.WORK, "work_id"),
    (NODE_LABELS.ARTIST, "artist_id"),
    (NODE_LABELS.RIGHTSHOLDER, "rightsholder_id"),
    (NODE_LABELS.PLATFORM, "platform_id"),
    (NODE_LABELS.CHART, "chart_id"),
    (NODE_LABELS.AWARD, "award_id"),
    (NODE_LABELS.CERTIFICATION, "certification_id"),
    (NODE_LABELS.SOCIAL_METRIC, "metric_id"),
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
