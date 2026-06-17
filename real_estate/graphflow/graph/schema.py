"""Neo4j 지식 그래프 스키마 — 노드/엣지 타입 + UNIQUE constraint.

테넌트/ESG/상권 3축 평가의 evidence가 모이는 그래프. 원본 raw rows는 SQLite에
잔존하고, 그래프엔 entity + aggregate edges + 수집물(WebDoc/Fact)만 둔다.
노드는 SQLite 자연 키를 식별자로 그대로 사용해 양방향 traceability 유지.

물리 그래프는 1개, 논리 2층 (설계 §3 원칙 1):
  - 전역 층: SQLite 적재 구조 노드 + 역대 수집 WebDoc/Fact (자산 간 공유)
  - 자산 스코프 층: Asset 노드 + ANCHORS 엣지로 전역 노드에 연결

레퍼런스: docs/design/graph_native_flow.md §4, [[tech_graph_stack]]
"""

from __future__ import annotations

from neo4j import Driver


# ───────────────────────────────────────────────────────────
# 노드 라벨 상수 (사용처 typo 방지)
# ───────────────────────────────────────────────────────────
class NODE_LABELS:
    # ── 전역 층: SQLite 적재 구조 노드 ──
    ASSET = "Asset"                      # 평가 대상 자산 — re:{bd_mgt_sn} | re:{lat},{lon}
    COMPLEX = "Complex"                  # apartment_complex.master_complex_id (배후수요)
    BUILDING = "Building"                # bdmgt_sn (cert/거래에서 등장)
    LEGAL_DONG = "LegalDong"             # bcode (10자리)
    ADMIN_DONG = "AdminDong"             # adstrd_cd
    COMMERCIAL_AREA = "CommercialArea"   # trdar_cd
    INDUSTRY = "Industry"                # svc 업종코드 (상권 매출 업종)
    DEVELOPMENT_PROJECT = "DevelopmentProject"
    CERTIFICATION = "Certification"      # BEEC/GSEED 통합 — cert_id
    TENANCY_EVENT = "TenancyEvent"       # MOLIT sale/rent 통합 — event_id
    TENANT = "Tenant"                    # 상가 입점업체(카카오 장소) — place_id
    MARKET_CONTEXT = "MarketContext"     # REB 지역 임대시장 환경 — market_id
    TRANSIT_STATION = "TransitStation"   # station_id

    # ── 확장 수집물 (Harvester/Navigator, PR-C~) ──
    WEB_DOC = "WebDoc"                   # 네이버 뉴스/블로그/웹 문서 — url_hash
    USER_DOC = "UserDoc"                 # 발행자/의뢰자 제공 자료 — doc_hash
    FACT = "Fact"                        # 원자적 사실 1문장 — fact_id
    ENTITY = "Entity"                    # 기존 라벨 매칭 실패 추출 엔티티 — entity_id
    QUERY = "Query"                      # 실행한 검색어 — query_hash

    # ── 보고서 grounding (PR-D~) ──
    REPORT = "Report"
    REPORT_SECTION = "ReportSection"
    CLAIM = "Claim"

    # ── 발행자 상호작용 (PR-F) ──
    FEEDBACK = "Feedback"                # 발행자 피드백 — feedback_id
    ASSESSMENT = "Assessment"            # 에이전트 회신 평가 — assessment_id
    QUESTION = "Question"                # QA 질문 — question_id


# ───────────────────────────────────────────────────────────
# 엣지 타입 상수
# ───────────────────────────────────────────────────────────
class EDGE_TYPES:
    # 구조 (Seeder)
    ANCHORS = "ANCHORS"                  # Asset → Building|LegalDong|AdminDong|CommercialArea
    LOCATED_IN = "LOCATED_IN"            # Building/Complex → LegalDong|AdminDong
    CERTIFIED_AS = "CERTIFIED_AS"        # Building → Certification
    HAS_TENANCY = "HAS_TENANCY"          # Complex → TenancyEvent
    HAS_TENANT = "HAS_TENANT"            # Asset → Tenant (상가 입점업체)
    HAS_MARKET = "HAS_MARKET"            # Asset → MarketContext (지역 임대시장 환경)
    SERVES_DISTRICT = "SERVES_DISTRICT"  # CommercialArea → AdminDong
    TOP_INDUSTRY = "TOP_INDUSTRY"        # CommercialArea → Industry
    WITHIN_RADIUS = "WITHIN_RADIUS"      # Asset → Complex|TransitStation (distance_m)

    # 확장 수집 (Harvester/Navigator)
    SPAWNED_QUERY = "SPAWNED_QUERY"      # 노드 일반 → Query
    RETRIEVED = "RETRIEVED"              # Query → WebDoc
    EXTRACTED_FROM = "EXTRACTED_FROM"    # Fact → WebDoc|UserDoc
    MENTIONS = "MENTIONS"                # Fact → 노드 일반 (확정 연결, 코드/주소 근거)
    SAME_AS_CANDIDATE = "SAME_AS_CANDIDATE"  # Entity → 노드 일반 (미확정, 저가중치)
    ABOUT = "ABOUT"                      # WebDoc → Entity|노드 일반

    # 보고서 grounding
    HAS_SECTION = "HAS_SECTION"          # Report → ReportSection
    HAS_CLAIM = "HAS_CLAIM"              # ReportSection → Claim
    CITES = "CITES"                      # Claim → 노드 일반 (증거)

    # 발행자 상호작용 + QA
    TARGETS = "TARGETS"                  # Feedback → Claim|ReportSection
    REPLIES_TO = "REPLIES_TO"            # Assessment → Feedback
    ASKED = "ASKED"                      # Question → Asset
    ANSWERED_BY = "ANSWERED_BY"          # Question → Claim|Fact
    GAP_AT = "GAP_AT"                    # Question → 노드 일반 (못 답한 위치)


# ───────────────────────────────────────────────────────────
# UNIQUE constraint — 자연 키 보장
# (라벨, 키 프로퍼티). idempotent 적재(MERGE)의 전제.
# ───────────────────────────────────────────────────────────
_UNIQUE_KEYS: list[tuple[str, str]] = [
    (NODE_LABELS.ASSET, "asset_id"),
    (NODE_LABELS.COMPLEX, "master_complex_id"),
    (NODE_LABELS.BUILDING, "bdmgt_sn"),
    (NODE_LABELS.LEGAL_DONG, "bcode"),
    (NODE_LABELS.ADMIN_DONG, "adstrd_cd"),
    (NODE_LABELS.COMMERCIAL_AREA, "trdar_cd"),
    (NODE_LABELS.INDUSTRY, "industry_cd"),
    (NODE_LABELS.DEVELOPMENT_PROJECT, "dev_id"),
    (NODE_LABELS.CERTIFICATION, "cert_id"),
    (NODE_LABELS.TENANCY_EVENT, "event_id"),
    (NODE_LABELS.TENANT, "place_id"),
    (NODE_LABELS.MARKET_CONTEXT, "market_id"),
    (NODE_LABELS.TRANSIT_STATION, "station_id"),
    (NODE_LABELS.WEB_DOC, "url_hash"),
    (NODE_LABELS.USER_DOC, "doc_hash"),
    (NODE_LABELS.FACT, "fact_id"),
    (NODE_LABELS.ENTITY, "entity_id"),
    (NODE_LABELS.QUERY, "query_hash"),
    (NODE_LABELS.REPORT, "report_id"),
    (NODE_LABELS.REPORT_SECTION, "section_id"),
    (NODE_LABELS.CLAIM, "claim_id"),
    (NODE_LABELS.FEEDBACK, "feedback_id"),
    (NODE_LABELS.ASSESSMENT, "assessment_id"),
    (NODE_LABELS.QUESTION, "question_id"),
]


def _constraint_name(label: str, key: str) -> str:
    return f"uniq_{label.lower()}_{key.lower()}"


CONSTRAINTS: list[str] = [
    f"CREATE CONSTRAINT {_constraint_name(label, key)} IF NOT EXISTS "
    f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE"
    for label, key in _UNIQUE_KEYS
]


# 라벨 → 키 프로퍼티 매핑 (제네릭 writer가 MERGE 키로 사용)
KEY_PROP: dict[str, str] = {label: key for label, key in _UNIQUE_KEYS}


def apply_constraints(driver: Driver) -> None:
    """모든 UNIQUE constraint 생성. idempotent."""
    with driver.session() as session:
        for cypher in CONSTRAINTS:
            session.run(cypher)
