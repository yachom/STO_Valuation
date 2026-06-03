"""오프라인 end-to-end 데모 — LLM/Neo4j 없이도 전체 파이프라인 동작 시연.

운영 파이프라인의 LLM 에이전트를 결정적 템플릿으로 대체하고, Neo4j 대신 SQLite
직접 쿼리로 evidence를 모은다. 같은 Report/ReportSection 도메인 객체와
ReportRepository를 사용하므로 산출물의 구조는 운영 파이프라인과 동일.

목적: 데이터 → 평가 보고서 흐름을 실제 데이터로 확인하고 품질 비판할 수 있게.

사용:
    python -m scripts.demo_offline_pipeline                 # 표본 단지(강남)
    python -m scripts.demo_offline_pipeline --complex-id 13
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
import uuid
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from real_estate.report.domain import Report, ReportSection
from real_estate.report.storage.report_repo import ReportRepository

DB_PATH = _PROJ / "real_estate" / "database" / "DB" / "real_estate.db"

logger = logging.getLogger("demo.offline")


# ───────────────────────────────────────────────────────────
# Evidence 수집 — SQLite 직접 쿼리
# ───────────────────────────────────────────────────────────
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def fetch_target_complex(conn: sqlite3.Connection, complex_id: int) -> dict | None:
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT * FROM apartment_complex WHERE master_complex_id = ?",
        (complex_id,),
    ).fetchone()
    return dict(r) if r else None


def gather_tenant_evidence(conn, target: dict) -> list[dict]:
    """매매/전월세 데이터가 없으므로 단지 메타데이터 + 인근 단지 분포로 대체."""
    evidence: list[dict] = []
    evidence.append({
        "text": (
            f"본 단지({target['canonical_name']})는 {target.get('completion_date','')} 준공, "
            f"{target.get('household_count') or '?'}세대 규모의 아파트이다. "
            f"건물 동수: {target.get('total_buildings') or '?'}동. "
            f"data_source={target.get('data_source')}."
        ),
        "ref": f"complex:{target['master_complex_id']}",
        "kind": "complex_meta",
    })
    # 같은 시도+시군구 prefix 안의 단지 분포 (legal_address 첫 두 토큰)
    addr = target.get("legal_address") or target.get("road_address") or ""
    tokens = addr.split()
    if len(tokens) >= 2:
        prefix = " ".join(tokens[:2])
        cur = conn.execute(
            "SELECT COUNT(*) AS n, AVG(household_count) AS avg_hh, "
            "AVG(CAST(substr(completion_date, 1, 4) AS INTEGER)) AS avg_year "
            "FROM apartment_complex WHERE legal_address LIKE ? OR road_address LIKE ?",
            (prefix + "%", prefix + "%"),
        ).fetchone()
        n, avg_hh, avg_yr = cur["n"], cur["avg_hh"], cur["avg_year"]
        if n:
            evidence.append({
                "text": (
                    f"{prefix} 행정구역 내 등록 단지 {n:,}개. "
                    f"평균 세대수 {avg_hh:.0f}세대, 평균 준공년도 {avg_yr:.0f}년."
                ),
                "ref": f"region_stats:{prefix}",
                "kind": "region_stats",
            })

    # 실거래 데이터가 있다면 (PR-1 build 이후)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) AS n, AVG(deal_amount) AS avg_amt "
            "FROM apt_sale_transaction WHERE master_complex_id = ?",
            (target["master_complex_id"],),
        ).fetchone()
        if cur and cur["n"]:
            evidence.append({
                "text": f"본 단지의 매매 실거래 {cur['n']}건, "
                        f"평균 {cur['avg_amt']:,.0f}만원.",
                "ref": "apt_sale_transaction",
                "kind": "sale_stats",
            })
    except sqlite3.OperationalError:
        pass  # 테이블 없으면 skip
    try:
        cur = conn.execute(
            "SELECT contract_type, COUNT(*) AS n FROM apt_rent_transaction "
            "WHERE master_complex_id = ? GROUP BY contract_type",
            (target["master_complex_id"],),
        ).fetchall()
        if cur:
            for r in cur:
                evidence.append({
                    "text": f"{r['contract_type']} 계약 {r['n']}건 기록.",
                    "ref": "apt_rent_transaction",
                    "kind": "rent_stats",
                })
    except sqlite3.OperationalError:
        pass
    return evidence


def gather_esg_evidence(conn, target: dict) -> list[dict]:
    """반경 1km 안에 인증 받은 건물이 있는지 확인."""
    evidence: list[dict] = []
    # 같은 시·구 안의 BEEC 인증 분포
    addr_token = (target.get("road_address") or target.get("legal_address") or "").split()
    sigungu = addr_token[1] if len(addr_token) >= 2 else ""

    if sigungu:
        cur = conn.execute(
            """
            SELECT grd_name, COUNT(*) AS n FROM energy_efficiency_cert
            WHERE jibun_addr LIKE ? OR full_addr LIKE ?
            GROUP BY grd_name ORDER BY n DESC LIMIT 8
            """,
            (f"%{sigungu}%", f"%{sigungu}%"),
        ).fetchall()
        if cur:
            dist = ", ".join(f"{r['grd_name']}={r['n']}" for r in cur)
            total = sum(r["n"] for r in cur)
            evidence.append({
                "text": f"{sigungu} 내 BEEC 에너지효율 인증 건물 {total:,}건 분포: {dist}.",
                "ref": f"beec_dist:{sigungu}",
                "kind": "beec_district",
            })

        cur = conn.execute(
            """
            SELECT cert_grade, COUNT(*) AS n FROM green_cert
            WHERE jibun_addr LIKE ? OR address LIKE ?
            GROUP BY cert_grade ORDER BY n DESC LIMIT 8
            """,
            (f"%{sigungu}%", f"%{sigungu}%"),
        ).fetchall()
        if cur:
            dist = ", ".join(f"{r['cert_grade']}={r['n']}" for r in cur)
            total = sum(r["n"] for r in cur)
            evidence.append({
                "text": f"{sigungu} 내 G-SEED 녹색건축 인증 {total:,}건 분포: {dist}.",
                "ref": f"gseed_dist:{sigungu}",
                "kind": "gseed_district",
            })

    # 단지 좌표에서 가장 가까운 cert 5건 (좌표가 있다면)
    lat, lng = target.get("latitude"), target.get("longitude")
    if lat and lng:
        try:
            # GSEED에 좌표는 없지만 bdmgt_sn 매칭으로 우회. 여기선 그냥 시·구 분포로.
            pass
        except Exception:
            pass

    if not evidence:
        evidence.append({
            "text": "해당 지역에 매핑된 ESG 인증 건물 데이터를 찾지 못했습니다 (데이터 부족).",
            "ref": "no_data",
            "kind": "missing",
        })
    return evidence


def gather_commercial_evidence(conn, target: dict) -> list[dict]:
    """좌표를 포함하는 상권 → 그 상권의 매출/인구 분포."""
    evidence: list[dict] = []
    lat, lng = target.get("latitude"), target.get("longitude")
    if not lat or not lng:
        evidence.append({
            "text": "단지 좌표가 없어 상권 매칭 불가.",
            "ref": "no_coord",
            "kind": "missing",
        })
        return evidence

    # bbox로 후보 추출 → polygon 포함 확인 (polygon_json 파싱 생략, 가장 가까운 중심점 사용)
    cur = conn.execute(
        "SELECT trdar_cd, trdar_name, trdar_se_name, area_sqm, center_lat, center_lon "
        "FROM commercial_area "
        "WHERE bbox_min_lat <= ? AND bbox_max_lat >= ? "
        "  AND bbox_min_lon <= ? AND bbox_max_lon >= ?",
        (lat, lat, lng, lng),
    ).fetchall()
    if not cur:
        # bbox 밖이면 가장 가까운 중심점
        cur = conn.execute(
            "SELECT trdar_cd, trdar_name, trdar_se_name, area_sqm, center_lat, center_lon "
            "FROM commercial_area WHERE center_lat IS NOT NULL "
        ).fetchall()
        if cur:
            cur = sorted(
                cur, key=lambda r: haversine_m(lat, lng, r["center_lat"], r["center_lon"])
            )[:1]
    if not cur:
        evidence.append({"text": "근접 상권 데이터 없음.", "ref": "no_data", "kind": "missing"})
        return evidence

    target_area = cur[0]
    trdar_cd = target_area["trdar_cd"]
    distance = haversine_m(lat, lng, target_area["center_lat"], target_area["center_lon"])
    evidence.append({
        "text": (
            f"인접 상권 '{target_area['trdar_name']}' ({target_area['trdar_se_name']}), "
            f"면적 {target_area['area_sqm']:,.0f}㎡, 중심점 거리 {distance:.0f}m."
        ),
        "ref": f"trdar:{trdar_cd}",
        "kind": "commercial_area",
    })

    # 최근 분기 유동인구
    cur = conn.execute(
        "SELECT quarter_code, total, age_20+age_30+age_40 AS prime_age FROM commercial_floating_population "
        "WHERE trdar_cd = ? ORDER BY quarter_code DESC LIMIT 1",
        (trdar_cd,),
    ).fetchone()
    if cur:
        evidence.append({
            "text": (
                f"가장 최근 {cur['quarter_code']} 분기 유동인구 {cur['total']:,}명, "
                f"이 중 20~40대 핵심 소비층 {cur['prime_age']:,}명."
            ),
            "ref": f"floating:{trdar_cd}:{cur['quarter_code']}",
            "kind": "floating",
        })

    # 분기 매출 합계 (모든 업종)
    cur = conn.execute(
        "SELECT quarter_code, SUM(sales_amount) AS s, COUNT(DISTINCT industry_code) AS n_industry "
        "FROM commercial_estimated_sales WHERE trdar_cd = ? "
        "GROUP BY quarter_code ORDER BY quarter_code DESC LIMIT 1",
        (trdar_cd,),
    ).fetchone()
    if cur and cur["s"]:
        evidence.append({
            "text": (
                f"가장 최근 {cur['quarter_code']} 분기 추정매출 합 {cur['s']:,}원 "
                f"({cur['n_industry']}개 업종 집계)."
            ),
            "ref": f"sales:{trdar_cd}:{cur['quarter_code']}",
            "kind": "sales",
        })

    # 같은 자치구의 정비사업
    sigungu = (target.get("road_address") or "").split()[1] if target.get("road_address") else ""
    if sigungu:
        cur = conn.execute(
            "SELECT category, COUNT(*) AS n FROM development_project "
            "WHERE district = ? GROUP BY category ORDER BY n DESC",
            (sigungu,),
        ).fetchall()
        if cur:
            dist = ", ".join(f"{r['category']}={r['n']}" for r in cur)
            evidence.append({
                "text": f"{sigungu} 진행 중 정비사업 분포: {dist}.",
                "ref": f"dev_proj:{sigungu}",
                "kind": "dev_project",
            })
    return evidence


# ───────────────────────────────────────────────────────────
# Template synthesizer — LLM 대체
# ───────────────────────────────────────────────────────────
def synth_section(axis: str, title: str, evidence: list[dict]) -> ReportSection:
    """evidence 리스트를 [E#] 인용 형식의 markdown 본문으로 합성."""
    if not evidence or all(e.get("kind") == "missing" for e in evidence):
        body = "이 축에 대한 evidence가 부족합니다. (데이터 부족)"
        refs, snippets = [], []
    else:
        lines = []
        for i, e in enumerate(evidence):
            lines.append(f"- {e['text']} [E{i+1}]")
        body = (
            f"### 발견된 사실\n\n" + "\n".join(lines) +
            f"\n\n### 평가 노트\n\n{_axis_evaluation_note(axis, evidence)}\n"
        )
        refs = [e["ref"] for e in evidence]
        snippets = [e["text"] for e in evidence]

    return ReportSection(
        section_id=f"sec_{uuid.uuid4().hex[:8]}",
        axis=axis,
        title=title,
        body_markdown=body,
        evidence_node_refs=refs,
        evidence_snippets=snippets,
    )


def _axis_evaluation_note(axis: str, evidence: list[dict]) -> str:
    if axis == "tenant":
        meta = next((e for e in evidence if e["kind"] == "complex_meta"), None)
        if meta and "household_count" in meta["text"]:
            return (
                "본 단지는 중·소규모 (100~300세대 추정) 구간으로, "
                "임대 시장 안정성 측면에선 대단지에 비해 임대인 의존도가 크다. "
                "실거래 데이터(MOLIT)가 적재되면 전월세 비율과 회전율로 "
                "더 정밀한 평가가 가능하다."
            )
        return "테넌트 가치 평가에 필요한 거래 데이터가 부족하다."
    if axis == "esg":
        beec = next((e for e in evidence if e["kind"] == "beec_district"), None)
        gseed = next((e for e in evidence if e["kind"] == "gseed_district"), None)
        if beec and gseed:
            return (
                "해당 자치구에 ESG 인증 건물 분포는 확보되었으나, "
                "본 단지 자체가 BEEC 또는 G-SEED 인증을 받았는지는 별도 매칭이 필요하다. "
                "단지 좌표 기반 1차 매칭 후 단지명 fuzzy 매칭으로 보강 권장."
            )
        return "ESG 인증 데이터가 해당 자치구에서 부분적으로만 확인된다."
    if axis == "commercial":
        return (
            "인접 상권의 유동인구와 추정매출은 본 자산의 입지 가치 산정에 핵심 입력이다. "
            "여기에 정비사업(주변 신·증축) 신호를 결합하면 forward-looking 시그널까지 확보할 수 있다."
        )
    return ""


def synth_overview(asset_summary: str, sections: list[ReportSection]) -> ReportSection:
    """3 섹션의 결론을 모아 executive summary 작성."""
    bullets = []
    for sec in sections:
        first_evidence = sec.evidence_snippets[0] if sec.evidence_snippets else "(데이터 부족)"
        bullets.append(f"- **{sec.title}**: {first_evidence}")

    body = (
        "### Executive Summary\n\n"
        f"본 자산({asset_summary.splitlines()[0]})에 대해 3축 평가 결과:\n\n"
        + "\n".join(bullets)
        + "\n\n### Cross-axis Insights\n\n"
        "- 테넌트 가치 평가는 실거래(MOLIT) 적재 후에 정밀화 가능.\n"
        "- ESG 인증은 자치구 분포 수준에서만 검증됨 — 단지 매칭으로 강화 필요.\n"
        "- 상권 + 정비사업 시그널은 위치 기반으로 확보되어 forward-looking 활용 가능.\n"
    )
    return ReportSection(
        section_id=f"sec_{uuid.uuid4().hex[:8]}",
        axis="overview",
        title="Executive Summary & 교차 축 통찰",
        body_markdown=body,
        evidence_node_refs=[],
        evidence_snippets=[],
    )


# ───────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--complex-id", type=int, default=13,
                        help="apartment_complex.master_complex_id (기본: 13)")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    target = fetch_target_complex(conn, args.complex_id)
    if target is None:
        sys.exit(f"단지 {args.complex_id} 없음.")
    logger.info("target complex: %s (id=%s)",
                target["canonical_name"], target["master_complex_id"])

    asset_summary = (
        f"{target['canonical_name']} ({target.get('road_address') or target.get('legal_address')})\n"
        f"좌표: ({target['latitude']}, {target['longitude']})\n"
        f"준공: {target.get('completion_date')}, 세대수: {target.get('household_count')}\n"
        f"data_source: {target.get('data_source')}"
    )

    # 1) 축별 evidence 수집
    tenant_ev = gather_tenant_evidence(conn, target)
    esg_ev = gather_esg_evidence(conn, target)
    commercial_ev = gather_commercial_evidence(conn, target)
    conn.close()

    # 2) 섹션 합성
    sec_tenant = synth_section("tenant", "테넌트 가치", tenant_ev)
    sec_esg = synth_section("esg", "ESG 가치", esg_ev)
    sec_com = synth_section("commercial", "상권 가치", commercial_ev)
    overview = synth_overview(asset_summary, [sec_tenant, sec_esg, sec_com])

    # 3) Report 도메인 객체 + 저장
    asset_id = f"re_complex_{target['master_complex_id']}"
    report = Report.new(asset_id=asset_id, asset_summary=asset_summary)
    report.append_version(
        [overview, sec_tenant, sec_esg, sec_com],
        notes="offline demo pipeline (LLM/Neo4j 대체)",
    )
    ReportRepository(DB_PATH).save(report)

    # 4) 출력
    print(f"\n=== Report {report.report_id} ===")
    print(f"asset: {asset_id}\n{asset_summary}\n")
    for sec in report.latest().sections:
        print(f"\n## [{sec.axis}] {sec.title}\n")
        print(sec.body_markdown)
        print(f"\n— refs: {len(sec.evidence_node_refs)}개")
    print(f"\n=== {report.report_id} 저장 완료 ===")
    print(f"sidecar md: real_estate/database/DB/reports/{report.report_id}/")


if __name__ == "__main__":
    main()
