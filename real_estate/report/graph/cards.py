"""노드 텍스트 카드 — Neo4j 노드별 1~3문장 한국어 요약을 `card_text` 프로퍼티로 저장.

retriever가 카드를 이용해 의미 검색 + LLM 컨텍스트 압축을 한다. 카드는 결정적
템플릿 기반으로 생성하므로 LLM 호출이 필요 없고 재현 가능하다 (PR-5 v1).
추후 PR-12에서 Haiku polish를 옵션으로 더할 수 있다 ([[tech_graph_stack]]).

각 노드 타입에 대해 builder 함수가 properties dict를 받아 Korean string을 반환.
None/누락 필드는 자연스럽게 생략한다.
"""

from __future__ import annotations

import logging

from neo4j import Driver

from real_estate.report.graph.schema import NODE_LABELS

logger = logging.getLogger("graph.cards")

_BATCH = 500


# ───────────────────────────────────────────────────────────
# 템플릿
# ───────────────────────────────────────────────────────────
def card_for_complex(p: dict) -> str:
    name = p.get("canonical_name", "이름 미상")
    parts = [f"{name}"]
    if p.get("road_address"):
        parts.append(f"({p['road_address']})")
    parts.append("는 ")
    sub = []
    if p.get("completion_date"):
        sub.append(f"{p['completion_date']} 준공")
    if p.get("household_count"):
        sub.append(f"{p['household_count']}세대 규모")
    if p.get("total_buildings"):
        sub.append(f"건물 {p['total_buildings']}동")
    parts.append(", ".join(sub) if sub else "정보 미확정")
    parts.append("의 아파트 단지이다.")
    return "".join(parts)


def card_for_building(p: dict) -> str:
    name = p.get("name", "건물명 미상")
    addr = p.get("jibun_addr") or p.get("addr") or ""
    suffix = f" ({addr})" if addr else ""
    return f"{name}{suffix} — 건물관리번호 {p.get('bdmgt_sn','')}."


def card_for_certification(p: dict) -> str:
    kind = p.get("kind", "")
    grade = p.get("grade", "")
    date = p.get("issue_date", "")
    if kind == "BEEC":
        extra = []
        if p.get("primary_energy_req") is not None:
            extra.append(f"1차에너지소요량 {p['primary_energy_req']}")
        if p.get("energy_reduce_rate") is not None:
            extra.append(f"에너지절감률 {p['energy_reduce_rate']}%")
        extra_s = " (" + ", ".join(extra) + ")" if extra else ""
        return f"BEEC 건물에너지효율 인증 — 등급 {grade}, 인증일 {date}{extra_s}."
    if kind == "GSEED":
        std = p.get("cert_standard", "")
        ct = p.get("cert_type", "")
        return f"G-SEED 녹색건축 {ct} — 등급 {grade}, 기준 {std}, 인증일 {date}."
    return f"인증 {kind} 등급 {grade} ({date})."


def card_for_tenancy_event(p: dict) -> str:
    kind = p.get("kind", "")
    ym = p.get("deal_year_month", "")
    area = p.get("area_sqm")
    area_s = f"{area}m²" if area else ""
    if kind == "sale":
        amount = p.get("amount")
        amount_s = f"{amount:,}만원" if amount else "금액 미상"
        return f"매매 거래 — {ym} {area_s} {amount_s}."
    elif kind == "rent":
        deposit = p.get("deposit") or 0
        monthly = p.get("monthly_rent") or 0
        ct = p.get("contract_type", "")
        if monthly == 0:
            return f"{ct} 거래 — {ym} {area_s} 보증금 {deposit:,}만원."
        return f"{ct} 거래 — {ym} {area_s} 보증금 {deposit:,}만원 / 월세 {monthly:,}만원."
    return f"거래 이벤트 ({ym})."


def card_for_commercial_area(p: dict) -> str:
    name = p.get("trdar_name", "상권명 미상")
    kind = p.get("trdar_se_name", "")
    area = p.get("area_sqm")
    area_s = f", 면적 {area:,.0f}㎡" if area else ""
    return f"상권 {name} ({kind}){area_s}."


def card_for_development_project(p: dict) -> str:
    name = p.get("name", "사업명 미상")
    district = p.get("district", "")
    cat = p.get("category", "")
    stage = p.get("stage", "")
    return f"{district} {name} — {cat} ({stage})."


def card_for_legal_dong(p: dict) -> str:
    return f"법정동 {p.get('bcode','')}."


def card_for_admin_dong(p: dict) -> str:
    name = p.get("adstrd_name") or ""
    return f"행정동 {name} ({p.get('adstrd_cd','')})."


_BUILDERS = {
    NODE_LABELS.COMPLEX: card_for_complex,
    NODE_LABELS.BUILDING: card_for_building,
    NODE_LABELS.CERTIFICATION: card_for_certification,
    NODE_LABELS.TENANCY_EVENT: card_for_tenancy_event,
    NODE_LABELS.COMMERCIAL_AREA: card_for_commercial_area,
    NODE_LABELS.DEVELOPMENT_PROJECT: card_for_development_project,
    NODE_LABELS.LEGAL_DONG: card_for_legal_dong,
    NODE_LABELS.ADMIN_DONG: card_for_admin_dong,
}


# ───────────────────────────────────────────────────────────
# 적용
# ───────────────────────────────────────────────────────────
def update_cards_for_label(
    driver: Driver,
    label: str,
    *,
    overwrite: bool = False,
) -> int:
    """주어진 라벨의 모든 노드(또는 card_text가 없는 노드만)에 카드 생성·저장."""
    builder = _BUILDERS.get(label)
    if builder is None:
        logger.warning("카드 빌더 미정의: %s", label)
        return 0

    where = "" if overwrite else "WHERE n.card_text IS NULL"
    with driver.session() as session:
        result = session.run(
            f"MATCH (n:{label}) {where} RETURN n, elementId(n) AS eid"
        )
        rows = list(result)

    updates: list[dict] = []
    for record in rows:
        node = record["n"]
        props = dict(node)
        card = builder(props)
        updates.append({"eid": record["eid"], "card": card})

    written = 0
    with driver.session() as session:
        for i in range(0, len(updates), _BATCH):
            chunk = updates[i : i + _BATCH]
            session.run(
                "UNWIND $rows AS row "
                "MATCH (n) WHERE elementId(n) = row.eid "
                "SET n.card_text = row.card",
                rows=chunk,
            )
            written += len(chunk)
    logger.info("Cards updated for %s: %d", label, written)
    return written


def update_all_cards(driver: Driver, *, overwrite: bool = False) -> dict:
    return {label: update_cards_for_label(driver, label, overwrite=overwrite)
            for label in _BUILDERS}
