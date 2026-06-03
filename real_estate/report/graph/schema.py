"""Neo4j 지식 그래프 스키마 — 노드/엣지 타입 + UNIQUE constraint Cypher.

테넌트/ESG/상권 3축 평가의 evidence가 모이는 그래프. 원본 raw rows는 SQLite에
잔존하고, 그래프엔 entity + aggregate edges + GraphRAG community만 두는 것이
원칙. 노드는 SQLite PK를 그대로 식별자로 사용해 양방향 traceability를 유지.

레퍼런스: [[tech_graph_stack]]
"""

from __future__ import annotations

from neo4j import Driver


# ───────────────────────────────────────────────────────────
# 노드 라벨 상수 (사용처에서 typo 방지용)
# ───────────────────────────────────────────────────────────
class NODE_LABELS:
    COMPLEX = "Complex"                  # apartment_complex.master_complex_id
    BUILDING = "Building"                # cert 테이블의 bdmgt_sn
    LEGAL_DONG = "LegalDong"             # bcode (10자리)
    ADMIN_DONG = "AdminDong"             # hjdong_cd / adstrd_cd
    COMMERCIAL_AREA = "CommercialArea"   # trdar_cd
    DEVELOPMENT_PROJECT = "DevelopmentProject"
    CERTIFICATION = "Certification"      # BEEC/GSEED 통합
    TENANCY_EVENT = "TenancyEvent"       # MOLIT sale/rent 통합
    # PR-2/3+에서 추가될 노드 라벨
    TRANSIT_STATION = "TransitStation"
    PARK = "Park"
    AIR_QUALITY_STATION = "AirQualityStation"
    ANCHOR_BUSINESS = "AnchorBusiness"
    # 보고서 grounding용 (PR-7에서 채워짐)
    REPORT = "Report"
    REPORT_SECTION = "ReportSection"
    CLAIM = "Claim"
    # GraphRAG community (PR-5에서 채워짐)
    COMMUNITY = "Community"


# ───────────────────────────────────────────────────────────
# 엣지 타입 상수
# ───────────────────────────────────────────────────────────
class EDGE_TYPES:
    LOCATED_IN = "LOCATED_IN"            # Complex/Building → AdminDong|LegalDong
    CERTIFIED_AS = "CERTIFIED_AS"        # Building → Certification
    HAS_TENANCY = "HAS_TENANCY"          # Complex → TenancyEvent
    SERVES_DISTRICT = "SERVES_DISTRICT"  # CommercialArea → AdminDong
    WITHIN_RADIUS = "WITHIN_RADIUS"      # Complex → TransitStation|Park|...
    # GraphRAG
    IN_COMMUNITY = "IN_COMMUNITY"        # * → Community
    # 보고서 grounding
    HAS_SECTION = "HAS_SECTION"          # Report → ReportSection
    HAS_CLAIM = "HAS_CLAIM"              # ReportSection → Claim
    CITES = "CITES"                      # Claim → 노드 일반


# ───────────────────────────────────────────────────────────
# UNIQUE constraint — 자연 키 보장
# Neo4j 5.x: CREATE CONSTRAINT IF NOT EXISTS FOR (n:Label) REQUIRE n.key IS UNIQUE
# ───────────────────────────────────────────────────────────
CONSTRAINTS = [
    f"CREATE CONSTRAINT complex_id_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.COMPLEX}) REQUIRE n.master_complex_id IS UNIQUE",

    f"CREATE CONSTRAINT building_bdmgt_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.BUILDING}) REQUIRE n.bdmgt_sn IS UNIQUE",

    f"CREATE CONSTRAINT legal_dong_bcode_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.LEGAL_DONG}) REQUIRE n.bcode IS UNIQUE",

    f"CREATE CONSTRAINT admin_dong_cd_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.ADMIN_DONG}) REQUIRE n.adstrd_cd IS UNIQUE",

    f"CREATE CONSTRAINT trdar_cd_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.COMMERCIAL_AREA}) REQUIRE n.trdar_cd IS UNIQUE",

    f"CREATE CONSTRAINT dev_proj_id_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.DEVELOPMENT_PROJECT}) REQUIRE n.id IS UNIQUE",

    f"CREATE CONSTRAINT cert_id_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.CERTIFICATION}) REQUIRE n.cert_id IS UNIQUE",

    f"CREATE CONSTRAINT tenancy_event_id_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.TENANCY_EVENT}) REQUIRE n.event_id IS UNIQUE",

    f"CREATE CONSTRAINT report_id_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.REPORT}) REQUIRE n.report_id IS UNIQUE",

    f"CREATE CONSTRAINT section_id_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.REPORT_SECTION}) REQUIRE n.section_id IS UNIQUE",

    f"CREATE CONSTRAINT claim_id_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.CLAIM}) REQUIRE n.claim_id IS UNIQUE",

    f"CREATE CONSTRAINT community_id_unique IF NOT EXISTS "
    f"FOR (n:{NODE_LABELS.COMMUNITY}) REQUIRE n.community_id IS UNIQUE",
]


def apply_constraints(driver: Driver) -> None:
    """모든 UNIQUE constraint를 생성. idempotent."""
    with driver.session() as session:
        for cypher in CONSTRAINTS:
            session.run(cypher)
