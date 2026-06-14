"""Janitor — 그래프 정비 배치 (설계 §13, D15).

수집 누적 시 인입 시점 방어(D9)만으로 못 막는 두 오류를 주기 점검한다:
    - 분열(duplicate): 같은 실체가 여러 Entity로 흩어짐 → 증거 분산
    - 오병합(conflation): 다른 실체가 한 노드로 → 인용 오염 (분열보다 위험)

LangGraph 워크플로우가 아니라 독립 배치 잡. 자동 처리는 **코드 수준 근거가
있을 때만** (이름 포함관계 + 안전 라벨). 위험 케이스(Asset/Building 타깃,
다중 실체)는 사람 확인 리뷰 큐로 — 보수적 기준.

처리 종류:
    A. 승격(promote): Entity-SAME_AS_CANDIDATE->(상권/행정동/업종/단지)에서
       Entity name이 타깃 name을 포함하면 Fact-MENTIONS를 타깃으로 이관하고
       Entity를 superseded 처리. "강남역 일대"·"강남역 사거리" → CommercialArea 강남역.
    B. 병합(merge): 두 Entity가 이름 포함관계 + 공통 candidate 타깃 → Fact 많은
       쪽으로 통합.
    C. 오병합 탐지: Entity가 Asset/Building을 가리키거나 2+ 다른 실체에 candidate
       → 리뷰 큐(needs_review 마킹). 자동 처리 안 함.

병합/승격 시 merged_into 기록 + card_embedding 제거(벡터 검색에서 제외).
traceability를 위해 superseded Entity 노드 자체는 보존한다.
"""

from __future__ import annotations

import logging
import re

from neo4j import Driver

from infra.agent.tracing import emit
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L

logger = logging.getLogger("graphflow.janitor")

_WS = re.compile(r"\s+")

# 흡수해도 안전한 타깃 라벨 (지역/상권/업종/단지 — 의미 보존)
_SAFE_PROMOTE_LABELS = {L.COMMERCIAL_AREA, L.ADMIN_DONG, L.INDUSTRY, L.COMPLEX}
# 오염 위험 — 자동 승격 금지, 리뷰 큐로
_RISKY_LABELS = {L.ASSET, L.BUILDING}
_MIN_NAME_LEN = 3


def _norm(s: str) -> str:
    return _WS.sub("", (s or "").strip().lower())


# ───────────────────────────────────────────────────────────
# A. 승격 (candidate Entity → 지역/상권/업종/단지 흡수)
# ───────────────────────────────────────────────────────────
def find_region_promotions(driver: Driver) -> list[dict]:
    """이름 포함 근거가 있는 안전한 승격 후보. 자동 처리 대상."""
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (e:{L.ENTITY})-[:{E.SAME_AS_CANDIDATE}]->(t)
            WHERE e.merged_into IS NULL AND labels(t)[0] IN $safe
            WITH e, t, labels(t)[0] AS tl,
                 coalesce(t.trdar_name, t.adstrd_name, t.industry_name,
                          t.canonical_name, t.name) AS tname
            WHERE tname IS NOT NULL
            OPTIONAL MATCH (f:{L.FACT})-[:{E.MENTIONS}]->(e)
            RETURN e.entity_id AS eid, e.name AS ename, tl, tname,
                   elementId(t) AS teid, count(DISTINCT f) AS n_facts
            """,
            safe=list(_SAFE_PROMOTE_LABELS),
        )
        out = []
        for r in rows:
            tnorm = _norm(r["tname"])
            enorm = _norm(r["ename"] or r["eid"])
            # 타깃 이름이 충분히 길고 Entity 이름이 그것을 포함해야 (같은 실체 근거)
            if len(tnorm) >= _MIN_NAME_LEN and tnorm in enorm:
                out.append({"eid": r["eid"], "ename": r["ename"], "label": r["tl"],
                            "tname": r["tname"], "teid": r["teid"],
                            "n_facts": r["n_facts"]})
        return out


def promote_entity(driver: Driver, entity_id: str, target_eid: str) -> int:
    """Fact-MENTIONS->Entity 를 Fact-MENTIONS->Target 으로 이관. Entity superseded.

    반환: 이관된 Fact 수.
    """
    with driver.session() as s:
        n = s.run(
            f"""
            MATCH (e:{L.ENTITY} {{entity_id: $eid}})
            MATCH (t) WHERE elementId(t) = $teid
            OPTIONAL MATCH (f:{L.FACT})-[m:{E.MENTIONS}]->(e)
            WITH e, t, collect(f) AS facts, collect(m) AS ms
            FOREACH (f IN facts | MERGE (f)-[:{E.MENTIONS}]->(t))
            FOREACH (m IN ms | DELETE m)
            WITH e, t, size(facts) AS moved
            OPTIONAL MATCH (e)-[c:{E.SAME_AS_CANDIDATE}]->()
            DELETE c
            SET e.merged_into = elementId(t), e.needs_review = false
            REMOVE e.card_embedding
            RETURN moved
            """,
            eid=entity_id, teid=target_eid,
        ).single()
        return n["moved"] if n else 0


# ───────────────────────────────────────────────────────────
# B. 병합 (Entity 분열 — 이름 포함 + 공통 타깃)
# ───────────────────────────────────────────────────────────
def find_duplicate_entities(driver: Driver) -> list[dict]:
    """이름 포함관계 + 공통 candidate 타깃을 가진 Entity 쌍 (같은 실체)."""
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (a:{L.ENTITY})-[:{E.SAME_AS_CANDIDATE}]->(t)<-[:{E.SAME_AS_CANDIDATE}]-(b:{L.ENTITY})
            WHERE a.entity_id < b.entity_id
              AND a.merged_into IS NULL AND b.merged_into IS NULL
              AND size(a.entity_id) >= {_MIN_NAME_LEN}
              AND size(b.entity_id) >= {_MIN_NAME_LEN}
              AND (a.entity_id CONTAINS b.entity_id OR b.entity_id CONTAINS a.entity_id)
            OPTIONAL MATCH (fa:{L.FACT})-[:{E.MENTIONS}]->(a)
            OPTIONAL MATCH (fb:{L.FACT})-[:{E.MENTIONS}]->(b)
            RETURN a.entity_id AS aid, a.name AS an, count(DISTINCT fa) AS fa,
                   b.entity_id AS bid, b.name AS bn, count(DISTINCT fb) AS fb
            """,
        )
        out = []
        for r in rows:
            # Fact 많은 쪽을 keep (증거 보존)
            if r["fa"] >= r["fb"]:
                out.append({"keep": r["aid"], "keep_name": r["an"],
                            "drop": r["bid"], "drop_name": r["bn"]})
            else:
                out.append({"keep": r["bid"], "keep_name": r["bn"],
                            "drop": r["aid"], "drop_name": r["an"]})
        return out


def merge_entities(driver: Driver, keep_id: str, drop_id: str) -> int:
    """drop Entity의 Fact-MENTIONS를 keep으로 이관. drop superseded."""
    with driver.session() as s:
        n = s.run(
            f"""
            MATCH (keep:{L.ENTITY} {{entity_id: $keep}})
            MATCH (drop:{L.ENTITY} {{entity_id: $drop}})
            OPTIONAL MATCH (f:{L.FACT})-[m:{E.MENTIONS}]->(drop)
            WITH keep, drop, collect(f) AS facts, collect(m) AS ms
            FOREACH (f IN facts | MERGE (f)-[:{E.MENTIONS}]->(keep))
            FOREACH (m IN ms | DELETE m)
            WITH keep, drop, size(facts) AS moved
            OPTIONAL MATCH (drop)-[c:{E.SAME_AS_CANDIDATE}]->()
            DELETE c
            SET drop.merged_into = elementId(keep)
            REMOVE drop.card_embedding
            RETURN moved
            """,
            keep=keep_id, drop=drop_id,
        ).single()
        return n["moved"] if n else 0


# ───────────────────────────────────────────────────────────
# C. 오병합 탐지 → 리뷰 큐 (자동 처리 안 함)
# ───────────────────────────────────────────────────────────
def find_conflations(driver: Driver) -> list[dict]:
    """위험 신호 Entity → 리뷰 큐 후보:
        ① Asset/Building을 candidate로 가리킴 (자산 오염 위험)
        ② 2+ 다른 실체(노드)에 candidate (동명 다실체)
    """
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (e:{L.ENTITY})-[:{E.SAME_AS_CANDIDATE}]->(t)
            WHERE e.merged_into IS NULL
            WITH e, collect(DISTINCT elementId(t)) AS targets,
                 collect(DISTINCT labels(t)[0]) AS labels
            WHERE size(targets) >= 2
               OR any(l IN labels WHERE l IN $risky)
            RETURN e.entity_id AS eid, e.name AS name, labels AS labels,
                   size(targets) AS n_targets
            """,
            risky=list(_RISKY_LABELS),
        )
        return [{"eid": r["eid"], "name": r["name"], "labels": r["labels"],
                 "n_targets": r["n_targets"]} for r in rows]


def mark_review(driver: Driver, entity_id: str, reason: str) -> None:
    with driver.session() as s:
        s.run(
            f"MATCH (e:{L.ENTITY} {{entity_id: $eid}}) "
            f"SET e.needs_review = true, e.review_reason = $reason",
            eid=entity_id, reason=reason)


# ───────────────────────────────────────────────────────────
# 오케스트레이션
# ───────────────────────────────────────────────────────────
def run_janitor(driver: Driver, *, auto: bool = True, dry_run: bool = False) -> dict:
    """그래프 정비 1회. auto=True면 코드 근거 케이스 자동 처리.

    반환: 정비 통계.
    """
    promotions = find_region_promotions(driver)
    duplicates = find_duplicate_entities(driver)
    conflations = find_conflations(driver)

    stats = {"promotions": len(promotions), "duplicates": len(duplicates),
             "conflations": len(conflations), "facts_moved": 0,
             "entities_superseded": 0, "review_queued": 0}

    if dry_run:
        stats["dry_run"] = True
        stats["promotion_samples"] = [
            f"{p['ename']} → {p['label']}:{p['tname']}" for p in promotions[:8]]
        stats["duplicate_samples"] = [
            f"{d['drop_name']} → {d['keep_name']}" for d in duplicates[:8]]
        stats["conflation_samples"] = [
            f"{c['name']} ({c['labels']}, {c['n_targets']}타깃)" for c in conflations[:8]]
        return stats

    if auto:
        # A. 승격 — 타깃이 코드 키 노드(상권/행정동/업종/단지)라 근거 있음 → 자동
        for p in promotions:
            moved = promote_entity(driver, p["eid"], p["teid"])
            stats["facts_moved"] += moved
            stats["entities_superseded"] += 1
        # B. 승격 후 남은 Entity끼리 분열 → 리뷰 큐 (코드 근거 없음, '논현역/신논현역'
        #    같은 접두 차이 오병합 위험이 커 자동 병합하지 않는다 — §13 보수 기준).
        #    merge_entities()는 수동 승인용 API로 남겨둔다.
        duplicates = find_duplicate_entities(driver)   # 승격된 것 제외 후 재탐지
        stats["duplicates"] = len(duplicates)
        for d in duplicates:
            mark_review(driver, d["drop"], f"duplicate_candidate→{d['keep']}")
            stats["review_queued"] += 1
        # C. 오병합 → 리뷰 큐
        for c in conflations:
            reason = ("multi_target" if c["n_targets"] >= 2
                      else "risky_target:" + ",".join(c["labels"]))
            mark_review(driver, c["eid"], reason)
            stats["review_queued"] += 1

    emit("janitor_done", **stats)
    logger.info("janitor: %s", stats)
    return stats
