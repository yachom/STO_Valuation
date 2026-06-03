"""SQLite (real_estate.db) → Neo4j 지식 그래프 ETL.

각 ingest 함수는:
    1. SQLite에서 한 도메인을 읽어 list[dict]로 변환
    2. UNWIND + MERGE 한 트랜잭션으로 노드/엣지 적재

PK는 SQLite의 자연 키 그대로 사용 (master_complex_id, bdmgt_sn, trdar_cd 등)
하여 양방향 traceability를 유지한다. raw 시계열(인구/매출)은 그래프엔
적재하지 않고 SQLite에 잔존 — 에이전트 시 필요할 때 SQLite를 조회한다.

전체 빌드는 ingest_all() 한 번에. idempotent — 재실행 안전.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from neo4j import Driver

from real_estate.report.graph.driver import get_neo4j_driver
from real_estate.report.graph.schema import EDGE_TYPES, NODE_LABELS, apply_constraints

logger = logging.getLogger("graph.ingest")

_BATCH = 500


# ───────────────────────────────────────────────────────────
# 헬퍼
# ───────────────────────────────────────────────────────────
def _open_sqlite(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _run_unwind(driver: Driver, cypher: str, rows: list[dict]) -> int:
    """UNWIND 한 트랜잭션으로 적재. batch 단위로 쪼개서 메모리/타임아웃 회피."""
    if not rows:
        return 0
    total = 0
    with driver.session() as session:
        for i in range(0, len(rows), _BATCH):
            chunk = rows[i : i + _BATCH]
            session.run(cypher, rows=chunk)
            total += len(chunk)
    return total


# ───────────────────────────────────────────────────────────
# 노드 ingest
# ───────────────────────────────────────────────────────────
def ingest_complex_nodes(conn: sqlite3.Connection, driver: Driver) -> int:
    rows = [dict(r) for r in conn.execute(
        "SELECT master_complex_id, canonical_name, road_address, legal_address, "
        "latitude, longitude, household_count, completion_date, total_buildings, "
        "seoul_complex_code, kb_complex_id, data_source "
        "FROM apartment_complex"
    )]
    cypher = f"""
        UNWIND $rows AS row
        MERGE (n:{NODE_LABELS.COMPLEX} {{master_complex_id: row.master_complex_id}})
        SET n.canonical_name = row.canonical_name,
            n.road_address = row.road_address,
            n.legal_address = row.legal_address,
            n.location = point({{latitude: row.latitude, longitude: row.longitude}}),
            n.household_count = row.household_count,
            n.completion_date = row.completion_date,
            n.total_buildings = row.total_buildings,
            n.seoul_complex_code = row.seoul_complex_code,
            n.kb_complex_id = row.kb_complex_id,
            n.data_source = row.data_source
    """
    n = _run_unwind(driver, cypher, rows)
    logger.info("Complex nodes: %d", n)
    return n


def ingest_legal_dong_nodes(conn: sqlite3.Connection, driver: Driver) -> int:
    """다양한 테이블에서 bcode 출처를 모아 LegalDong 노드를 만든다."""
    bcodes = set()
    for table, col in [
        ("apt_sale_transaction", "bcode"),
        ("apt_rent_transaction", "bcode"),
    ]:
        if _table_exists(conn, table):
            cur = conn.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"
            )
            bcodes.update(r[0] for r in cur if r[0])
    rows = [{"bcode": b} for b in sorted(bcodes)]
    cypher = f"""
        UNWIND $rows AS row
        MERGE (n:{NODE_LABELS.LEGAL_DONG} {{bcode: row.bcode}})
    """
    n = _run_unwind(driver, cypher, rows)
    logger.info("LegalDong nodes: %d", n)
    return n


def ingest_admin_dong_nodes(conn: sqlite3.Connection, driver: Driver) -> int:
    """commercial_area.adstrd_cd + admin_dong_* 시계열의 adstrd_cd."""
    codes = {}
    if _table_exists(conn, "commercial_area"):
        cur = conn.execute(
            "SELECT DISTINCT adstrd_cd, adstrd_name FROM commercial_area "
            "WHERE adstrd_cd IS NOT NULL"
        )
        for r in cur:
            codes[r["adstrd_cd"]] = r["adstrd_name"]
    for table in ("admin_dong_floating_population", "admin_dong_resident_population",
                  "admin_dong_estimated_sales"):
        if _table_exists(conn, table):
            cur = conn.execute(
                f"SELECT DISTINCT adstrd_cd, adstrd_name FROM {table} "
                "WHERE adstrd_cd IS NOT NULL"
            )
            for r in cur:
                codes.setdefault(r["adstrd_cd"], r["adstrd_name"])
    rows = [{"adstrd_cd": cd, "adstrd_name": nm} for cd, nm in sorted(codes.items())]
    cypher = f"""
        UNWIND $rows AS row
        MERGE (n:{NODE_LABELS.ADMIN_DONG} {{adstrd_cd: row.adstrd_cd}})
        SET n.adstrd_name = row.adstrd_name
    """
    n = _run_unwind(driver, cypher, rows)
    logger.info("AdminDong nodes: %d", n)
    return n


def ingest_commercial_area_nodes(conn: sqlite3.Connection, driver: Driver) -> int:
    rows = [dict(r) for r in conn.execute(
        "SELECT trdar_cd, trdar_name, trdar_se_name, signgu_cd, adstrd_cd, "
        "area_sqm, center_lon, center_lat FROM commercial_area"
    )]
    cypher = f"""
        UNWIND $rows AS row
        MERGE (n:{NODE_LABELS.COMMERCIAL_AREA} {{trdar_cd: row.trdar_cd}})
        SET n.trdar_name = row.trdar_name,
            n.trdar_se_name = row.trdar_se_name,
            n.signgu_cd = row.signgu_cd,
            n.adstrd_cd = row.adstrd_cd,
            n.area_sqm = row.area_sqm,
            n.center = CASE WHEN row.center_lat IS NOT NULL AND row.center_lon IS NOT NULL
                            THEN point({{latitude: row.center_lat, longitude: row.center_lon}})
                            ELSE null END
    """
    n = _run_unwind(driver, cypher, rows)
    logger.info("CommercialArea nodes: %d", n)
    return n


def ingest_dev_project_nodes(conn: sqlite3.Connection, driver: Driver) -> int:
    rows = [dict(r) for r in conn.execute(
        "SELECT id, district, category, name, jibun, stage, operation_kind, operation_stage "
        "FROM development_project"
    )]
    cypher = f"""
        UNWIND $rows AS row
        MERGE (n:{NODE_LABELS.DEVELOPMENT_PROJECT} {{id: row.id}})
        SET n.district = row.district,
            n.category = row.category,
            n.name = row.name,
            n.jibun = row.jibun,
            n.stage = row.stage,
            n.operation_kind = row.operation_kind,
            n.operation_stage = row.operation_stage
    """
    n = _run_unwind(driver, cypher, rows)
    logger.info("DevelopmentProject nodes: %d", n)
    return n


def ingest_building_and_certifications(conn: sqlite3.Connection, driver: Driver) -> tuple[int, int]:
    """건물 마스터 노드 + 인증 노드 + CERTIFIED_AS 엣지."""
    # 1) bdmgt_sn 마스터 (cert 테이블 두 곳의 합집합)
    bdmgt_meta: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT bdmgt_sn, bld_nm AS name, full_addr AS addr, jibun_addr "
        "FROM energy_efficiency_cert WHERE bdmgt_sn IS NOT NULL AND bdmgt_sn != ''"
    ):
        bdmgt_meta.setdefault(r["bdmgt_sn"], {"name": r["name"], "addr": r["addr"], "jibun_addr": r["jibun_addr"]})
    for r in conn.execute(
        "SELECT bdmgt_sn, building_name AS name, address AS addr, jibun_addr "
        "FROM green_cert WHERE bdmgt_sn IS NOT NULL AND bdmgt_sn != ''"
    ):
        bdmgt_meta.setdefault(r["bdmgt_sn"], {"name": r["name"], "addr": r["addr"], "jibun_addr": r["jibun_addr"]})
    building_rows = [{"bdmgt_sn": k, **v} for k, v in bdmgt_meta.items()]
    cypher = f"""
        UNWIND $rows AS row
        MERGE (n:{NODE_LABELS.BUILDING} {{bdmgt_sn: row.bdmgt_sn}})
        SET n.name = coalesce(n.name, row.name),
            n.addr = coalesce(n.addr, row.addr),
            n.jibun_addr = coalesce(n.jibun_addr, row.jibun_addr)
    """
    n_buildings = _run_unwind(driver, cypher, building_rows)
    logger.info("Building nodes: %d", n_buildings)

    # 2) Certification 노드 + 엣지 (BEEC)
    beec_rows = [
        {
            "cert_id": f"BEEC:{r['rqid']}",
            "kind": "BEEC",
            "grade": r["grd_name"],
            "issue_date": r["crtif_isu_dd"],
            "bdmgt_sn": r["bdmgt_sn"],
            "primary_energy_req": r["primary_energy_req"],
            "energy_reduce_rate": r["energy_reduce_rate"],
        }
        for r in conn.execute(
            "SELECT rqid, grd_name, crtif_isu_dd, bdmgt_sn, primary_energy_req, energy_reduce_rate "
            "FROM energy_efficiency_cert WHERE bdmgt_sn IS NOT NULL AND bdmgt_sn != ''"
        )
    ]
    # GSEED
    gseed_rows = [
        {
            "cert_id": f"GSEED:{r['cert_no']}",
            "kind": "GSEED",
            "grade": r["cert_grade"],
            "issue_date": r["cert_date"],
            "bdmgt_sn": r["bdmgt_sn"],
            "cert_standard": r["cert_standard"],
            "cert_type": r["cert_type"],
        }
        for r in conn.execute(
            "SELECT cert_no, cert_grade, cert_date, bdmgt_sn, cert_standard, cert_type "
            "FROM green_cert WHERE bdmgt_sn IS NOT NULL AND bdmgt_sn != ''"
        )
    ]
    all_certs = beec_rows + gseed_rows
    cypher = f"""
        UNWIND $rows AS row
        MERGE (c:{NODE_LABELS.CERTIFICATION} {{cert_id: row.cert_id}})
        SET c.kind = row.kind,
            c.grade = row.grade,
            c.issue_date = row.issue_date,
            c.primary_energy_req = row.primary_energy_req,
            c.energy_reduce_rate = row.energy_reduce_rate,
            c.cert_standard = row.cert_standard,
            c.cert_type = row.cert_type
        WITH c, row
        MATCH (b:{NODE_LABELS.BUILDING} {{bdmgt_sn: row.bdmgt_sn}})
        MERGE (b)-[:{EDGE_TYPES.CERTIFIED_AS}]->(c)
    """
    n_certs = _run_unwind(driver, cypher, all_certs)
    logger.info("Certification nodes + CERTIFIED_AS: %d", n_certs)
    return n_buildings, n_certs


def ingest_tenancy_events(conn: sqlite3.Connection, driver: Driver) -> int:
    """매매(sale) + 전월세(rent) → TenancyEvent + HAS_TENANCY 엣지.

    master_complex_id가 NULL인 row는 그래프에 넣어도 단지 연결이 안 되므로 skip.
    """
    rows: list[dict] = []
    if _table_exists(conn, "apt_sale_transaction"):
        for r in conn.execute(
            "SELECT transaction_id, master_complex_id, deal_year_month, deal_day, "
            "deal_amount, area_sqm, floor, build_year, is_canceled "
            "FROM apt_sale_transaction WHERE master_complex_id IS NOT NULL"
        ):
            rows.append({
                "event_id": f"SALE:{r['transaction_id']}",
                "kind": "sale",
                "master_complex_id": r["master_complex_id"],
                "deal_year_month": r["deal_year_month"],
                "deal_day": r["deal_day"],
                "amount": r["deal_amount"],          # 만원
                "deposit": None,
                "monthly_rent": None,
                "area_sqm": r["area_sqm"],
                "floor": r["floor"],
                "build_year": r["build_year"],
                "is_canceled": bool(r["is_canceled"]),
                "contract_type": "매매",
            })
    if _table_exists(conn, "apt_rent_transaction"):
        for r in conn.execute(
            "SELECT rent_id, master_complex_id, deal_year_month, deal_day, "
            "deposit, monthly_rent, contract_type, area_sqm, floor, build_year "
            "FROM apt_rent_transaction WHERE master_complex_id IS NOT NULL"
        ):
            rows.append({
                "event_id": f"RENT:{r['rent_id']}",
                "kind": "rent",
                "master_complex_id": r["master_complex_id"],
                "deal_year_month": r["deal_year_month"],
                "deal_day": r["deal_day"],
                "amount": None,
                "deposit": r["deposit"],
                "monthly_rent": r["monthly_rent"],
                "area_sqm": r["area_sqm"],
                "floor": r["floor"],
                "build_year": r["build_year"],
                "is_canceled": False,
                "contract_type": r["contract_type"],
            })
    cypher = f"""
        UNWIND $rows AS row
        MERGE (e:{NODE_LABELS.TENANCY_EVENT} {{event_id: row.event_id}})
        SET e.kind = row.kind,
            e.deal_year_month = row.deal_year_month,
            e.deal_day = row.deal_day,
            e.amount = row.amount,
            e.deposit = row.deposit,
            e.monthly_rent = row.monthly_rent,
            e.area_sqm = row.area_sqm,
            e.floor = row.floor,
            e.build_year = row.build_year,
            e.is_canceled = row.is_canceled,
            e.contract_type = row.contract_type
        WITH e, row
        MATCH (c:{NODE_LABELS.COMPLEX} {{master_complex_id: row.master_complex_id}})
        MERGE (c)-[:{EDGE_TYPES.HAS_TENANCY}]->(e)
    """
    n = _run_unwind(driver, cypher, rows)
    logger.info("TenancyEvent nodes + HAS_TENANCY: %d", n)
    return n


# ───────────────────────────────────────────────────────────
# 엣지 ingest (구조 관계)
# ───────────────────────────────────────────────────────────
def ingest_commercial_area_to_admin_dong_edges(conn: sqlite3.Connection, driver: Driver) -> int:
    rows = [
        {"trdar_cd": r["trdar_cd"], "adstrd_cd": r["adstrd_cd"]}
        for r in conn.execute(
            "SELECT trdar_cd, adstrd_cd FROM commercial_area WHERE adstrd_cd IS NOT NULL"
        )
    ]
    cypher = f"""
        UNWIND $rows AS row
        MATCH (ca:{NODE_LABELS.COMMERCIAL_AREA} {{trdar_cd: row.trdar_cd}})
        MATCH (ad:{NODE_LABELS.ADMIN_DONG} {{adstrd_cd: row.adstrd_cd}})
        MERGE (ca)-[:{EDGE_TYPES.SERVES_DISTRICT}]->(ad)
    """
    n = _run_unwind(driver, cypher, rows)
    logger.info("SERVES_DISTRICT: %d", n)
    return n


# ───────────────────────────────────────────────────────────
# 보조
# ───────────────────────────────────────────────────────────
def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


# ───────────────────────────────────────────────────────────
# 일괄 빌드
# ───────────────────────────────────────────────────────────
def ingest_all(
    db_path: str | Path,
    *,
    driver: Driver | None = None,
) -> dict:
    """전체 그래프 적재. idempotent (MERGE 기반).

    SQLite 파일 경로를 받고, NEO4J_* env로부터 driver를 자동 생성한다.
    호출자는 종료 시 close_neo4j_driver()를 부르거나, 명시적으로 driver를
    전달해 lifecycle을 직접 관리할 수 있다.
    """
    if driver is None:
        driver = get_neo4j_driver()
    apply_constraints(driver)

    conn = _open_sqlite(db_path)
    try:
        summary = {
            "complex": ingest_complex_nodes(conn, driver),
            "legal_dong": ingest_legal_dong_nodes(conn, driver),
            "admin_dong": ingest_admin_dong_nodes(conn, driver),
            "commercial_area": ingest_commercial_area_nodes(conn, driver),
            "development_project": ingest_dev_project_nodes(conn, driver),
        }
        n_buildings, n_certs = ingest_building_and_certifications(conn, driver)
        summary["buildings"] = n_buildings
        summary["certifications"] = n_certs
        summary["tenancy_events"] = ingest_tenancy_events(conn, driver)
        summary["serves_district_edges"] = ingest_commercial_area_to_admin_dong_edges(conn, driver)
    finally:
        conn.close()

    logger.info("ingest_all DONE: %s", summary)
    return summary
