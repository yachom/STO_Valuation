"""Navigator — EXPAND 루프의 frontier 선정 + 경로 기반 검색어 생성 (설계 §8.1/§8.2).

라운드당 LLM 1회 (토의 확정): 입력 = frontier 후보 노드 카드 + 템플릿 쿼리 +
기실행 쿼리 목록 → 출력 = 확장 타깃 ≤M개와 각 타깃의 검색어 ≤2개.

안전장치는 전부 코드에 있다:
    - frontier 후보·점수는 walk 기반 결정적 계산 (LLM은 후보 안에서만 고름)
    - 검색어에 해당 노드의 고유명사가 문자 그대로 없으면 폐기 → 템플릿으로 대체
    - 기실행 쿼리와 정규화 텍스트 중복 시 폐기
    - Query 노드 적재로 provenance + 라운드 간 중복 차단
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from neo4j import Driver
from pydantic import BaseModel, Field

from real_estate.graphflow.agents.base import BaseAgent, normalize_text, short_hash
from real_estate.graphflow.prompts import get_prompt
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import KEY_PROP
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.policies import StaticPolicy
from infra.agent.tracing import emit
from real_estate.graphflow.traversal import asset_anchor_eid, walk

logger = logging.getLogger("graphflow.navigator")

# 검색해볼 가치가 있는(시의성 있는 외부 정보가 존재할 법한) 라벨만 frontier 후보.
# Certification/TenancyEvent/LegalDong/TransitStation은 정적 → 제외.
_EXPANDABLE_LABELS = {
    L.ASSET, L.BUILDING, L.COMMERCIAL_AREA, L.INDUSTRY, L.ADMIN_DONG,
    L.COMPLEX, L.DEVELOPMENT_PROJECT, L.ENTITY,
}

# frontier 우선순위 — 평가 대상은 '상가 자산'이므로 상권/업종/건물/자산/개발에
# 검색 예산을 집중하고, 배후수요 아파트(Complex)는 보조로 억제한다. 진단상
# Complex가 facts=0·고가치 path로 frontier 상위를 장악해 '아파트 시세' 뉴스가
# 수집을 독식하던 것을 교정 (상가 가치의 LG/MICE/공실 Fact가 묻히던 원인).
_FRONTIER_LABEL_PRIORITY = {
    L.ASSET: 1.0, L.COMMERCIAL_AREA: 1.0, L.INDUSTRY: 1.0, L.BUILDING: 1.0,
    L.DEVELOPMENT_PROJECT: 0.9, L.ENTITY: 0.9, L.ADMIN_DONG: 0.8,
    L.COMPLEX: 0.35,   # 배후수요 아파트 — 보조 (시세 뉴스 예산 독식 억제)
}


# ───────────────────────────────────────────────────────────
# frontier 후보
# ───────────────────────────────────────────────────────────
@dataclass
class FrontierCandidate:
    eid: str
    label: str
    key: str
    card: str
    props: dict
    path_score: float
    n_facts: int = 0
    n_queries: int = 0
    proper_nouns: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.label}:{self.key}"

    @property
    def frontier_score(self) -> float:
        # 자산과 가깝고(path_score), 증거가 빈약하고(n_facts), 아직 안 판(n_queries)
        # × 라벨 우선순위(상가 가치 관련 노드 우선, 배후수요 아파트 억제)
        lp = _FRONTIER_LABEL_PRIORITY.get(self.label, 0.7)
        return lp * self.path_score / (1 + self.n_facts) / (1 + 0.5 * self.n_queries)


def fetch_props(driver: Driver, eids: list[str]) -> dict[str, dict]:
    """노드 프로퍼티 조회 (card_embedding 제외 — 1024-dim 전송 회피)."""
    if not eids:
        return {}
    with driver.session() as s:
        result = s.run(
            """
            UNWIND $eids AS eid
            MATCH (n) WHERE elementId(n) = eid
            RETURN eid, [k IN keys(n) WHERE k <> 'card_embedding' | [k, n[k]]] AS kv
            """,
            eids=eids,
        )
        return {r["eid"]: dict(r["kv"]) for r in result}


def _fetch_evidence_counts(driver: Driver, eids: list[str]) -> dict[str, tuple[int, int]]:
    if not eids:
        return {}
    with driver.session() as s:
        result = s.run(
            f"""
            UNWIND $eids AS eid
            MATCH (n) WHERE elementId(n) = eid
            OPTIONAL MATCH (f:{L.FACT})-[:{E.MENTIONS}]->(n)
            WITH eid, n, count(DISTINCT f) AS n_facts
            OPTIONAL MATCH (n)-[:{E.SPAWNED_QUERY}]->(q:{L.QUERY})
            RETURN eid, n_facts, count(DISTINCT q) AS n_queries
            """,
            eids=eids,
        )
        return {r["eid"]: (r["n_facts"], r["n_queries"]) for r in result}


def fetch_executed_queries(driver: Driver, asset_id: str, *, hops: int = 3) -> list[str]:
    """자산 스코프에서 이미 실행된 검색어 텍스트 목록."""
    with driver.session() as s:
        result = s.run(
            f"""
            MATCH (a:{L.ASSET} {{asset_id: $aid}})-[*0..{int(hops)}]-(n)
                  -[:{E.SPAWNED_QUERY}]->(q:{L.QUERY})
            RETURN DISTINCT q.text AS text
            """,
            aid=asset_id,
        )
        return [r["text"] for r in result]


# ───────────────────────────────────────────────────────────
# 경로 템플릿 전개 (결정적) — 라벨별 고유명사 슬롯 + 쿼리 템플릿
# ───────────────────────────────────────────────────────────
def proper_nouns_for(label: str, props: dict, asset_props: dict) -> list[str]:
    """이 노드를 특정하는 고유명사들 — 검색어 포함 강제 대상."""
    out: list[str] = []

    def add(*vals):
        for v in vals:
            if v and isinstance(v, str) and len(v) >= 2 and v not in out:
                out.append(v)

    if label == L.ASSET:
        from real_estate.location import core_road_address
        # 풀주소 + 도로명+번호 코어 둘 다 — LLM/템플릿이 시/구 접두 없이 '공항대로
        # 209'만 쓰는 경우에도 고유명사 매칭이 통과하도록 (도로명 불일치 방지).
        add(props.get("building_name"), props.get("road_address"),
            core_road_address(props.get("road_address") or ""),
            props.get("jibun_address"), props.get("bname"))
    elif label == L.BUILDING:
        name = props.get("name")
        add(name if name and name != "건물명 미상" else None, props.get("jibun_addr"))
    elif label == L.COMMERCIAL_AREA:
        add(props.get("trdar_name"))
    elif label == L.INDUSTRY:
        add(props.get("industry_name"), props.get("trdar_name"))
    elif label == L.ADMIN_DONG:
        add(props.get("adstrd_name"))
    elif label == L.COMPLEX:
        add(props.get("canonical_name"))
    elif label == L.DEVELOPMENT_PROJECT:
        add(props.get("name"), props.get("district"))
    elif label == L.ENTITY:
        add(props.get("name"), props.get("entity_id"))
    return out


def template_queries(label: str, props: dict, asset_props: dict) -> list[str]:
    """라벨별 결정적 쿼리 템플릿 — 경로 위 고유명사가 슬롯에 박힌다."""
    sigungu = asset_props.get("sigungu", "")
    bname = asset_props.get("bname", "")

    if label == L.COMMERCIAL_AREA:
        nm = props.get("trdar_name")
        return [f"{nm} 상권 공실", f"{nm} 상권 동향"] if nm else []
    if label == L.INDUSTRY:
        ind = props.get("industry_name")
        trdar = props.get("trdar_name") or sigungu
        return [f"{trdar} {ind} 출점", f"{sigungu} {ind} 폐업"] if ind else []
    if label == L.ADMIN_DONG:
        nm = props.get("adstrd_name")
        return [f"{nm} 상가 임대료", f"{nm} 개발"] if nm else []
    if label == L.ASSET:
        nm = props.get("building_name") or props.get("road_address")
        out = [f"{nm} 상가"] if nm else []
        if sigungu and bname:
            out.append(f"{sigungu} {bname} 상권")
        return out
    if label == L.BUILDING:
        nm = props.get("name")
        return [f"{nm} 임대"] if nm and nm != "건물명 미상" else []
    if label == L.COMPLEX:
        nm = props.get("canonical_name")
        return [f"{nm} 재건축", f"{nm} 시세"] if nm else []
    if label == L.DEVELOPMENT_PROJECT:
        nm = props.get("name")
        return [f"{nm} 진행", f"{nm} 분양"] if nm else []
    if label == L.ENTITY:
        nm = props.get("name") or props.get("entity_id")
        return [f"{nm} 동향"] if nm else []
    return []


def collect_frontier(
    driver: Driver,
    asset_id: str,
    *,
    max_hops: int = 2,
    limit: int = 12,
) -> list[FrontierCandidate]:
    """walk 기반 frontier 후보 수집 (결정적). 점수순 상위 limit개."""
    anchor = asset_anchor_eid(driver, asset_id)
    if anchor is None:
        raise ValueError(f"Asset 노드 없음: {asset_id} — Seeder 먼저 실행")

    paths = walk(driver, [anchor], StaticPolicy("qa_base"), max_hops=max_hops, k=50)
    # tail 노드별 path_score + Asset 자신도 후보 (자산 직접 검색용)
    tails: dict[str, tuple[str, str, str, float]] = {
        anchor: (L.ASSET, asset_id, "", 1.0)}
    for p in paths:
        t = p.tail
        if t.label in _EXPANDABLE_LABELS and t.eid not in tails:
            tails[t.eid] = (t.label, t.key, t.card_text, p.score)

    eids = list(tails)
    props_map = fetch_props(driver, eids)
    counts = _fetch_evidence_counts(driver, eids)
    asset_props = props_map.get(anchor, {})

    cands: list[FrontierCandidate] = []
    for eid, (label, key, card, score) in tails.items():
        props = props_map.get(eid, {})
        nouns = proper_nouns_for(label, props, asset_props)
        templates = template_queries(label, props, asset_props)
        if not nouns or not templates:     # 검색어를 만들 고유명사가 없으면 제외
            continue
        n_facts, n_queries = counts.get(eid, (0, 0))
        if not card:
            card = props.get("card_text", "")
        cands.append(FrontierCandidate(
            eid=eid, label=label, key=key, card=card, props=props,
            path_score=score, n_facts=n_facts, n_queries=n_queries,
            proper_nouns=nouns, templates=templates,
        ))
    cands.sort(key=lambda c: (-c.frontier_score, c.ref))
    return cands[:limit]


# ───────────────────────────────────────────────────────────
# Navigator 에이전트 (LLM 1회: 타깃 선정 + 검색어 다듬기)
# ───────────────────────────────────────────────────────────
class QueryPlan(BaseModel):
    target: str = Field(description="후보 목록의 ref 그대로 (예: 'CommercialArea:3120189')")
    queries: list[str] = Field(description="네이버 뉴스 검색어 1~2개. 반드시 해당 노드의 고유명사를 문자 그대로 포함")
    why: str = Field(description="이 노드를 지금 더 파야 하는 이유 한 문장")


class NavigatorOutput(BaseModel):
    plans: list[QueryPlan] = Field(description="확장할 타깃들 (중요도 순)")


class Navigator(BaseAgent):
    name = "navigator"

    def plan_round(
        self,
        asset_card: str,
        candidates: list[FrontierCandidate],
        executed: list[str],
        *,
        max_targets: int = 8,
        queries_per_node: int = 2,
        purpose: str | None = None,    # QA 미니 EXPAND: 섹션 주제 대신 질문 (§8.5)
    ) -> list[QueryPlan]:
        """frontier 후보 → 확장 계획. LLM 1회 + 코드 검증."""
        cand_lines = []
        for c in candidates:
            cand_lines.append(
                f"- ref={c.ref} | 기존 Fact {c.n_facts}건, 기실행 검색 {c.n_queries}건\n"
                f"  카드: {c.card}\n"
                f"  고유명사: {', '.join(c.proper_nouns)}\n"
                f"  템플릿 제안: {' / '.join(c.templates)}"
            )
        executed_block = "\n".join(f"- {q}" for q in executed) if executed else "(없음)"
        purpose_block = (
            f"[이번 확장의 목적 — 이 질문에 답할 정보 수집이 최우선]\n{purpose}\n\n"
            if purpose else "")
        user = (
            f"[평가 대상 자산]\n{asset_card}\n\n"
            + purpose_block
            + f"[frontier 후보 — frontier 점수순]\n" + "\n".join(cand_lines) + "\n\n"
            f"[이미 실행된 검색어]\n{executed_block}\n\n"
            f"최대 {max_targets}개 타깃을 고르고, 타깃당 검색어 최대 {queries_per_node}개를 "
            f"만들어 주세요."
        )
        out: NavigatorOutput = self.complete(
            system=get_prompt("navigator.system"), user=user, schema=NavigatorOutput)
        return self._validate(out.plans, candidates, executed,
                              max_targets=max_targets, queries_per_node=queries_per_node)

    @staticmethod
    def _validate(
        plans: list[QueryPlan],
        candidates: list[FrontierCandidate],
        executed: list[str],
        *,
        max_targets: int,
        queries_per_node: int,
    ) -> list[QueryPlan]:
        """코드 레벨 안전장치 — 고유명사 포함/중복/타깃 실재 검증."""
        by_ref = {c.ref: c for c in candidates}
        seen_norm = {normalize_text(q) for q in executed}
        validated: list[QueryPlan] = []
        for plan in plans[:max_targets]:
            cand = by_ref.get(plan.target)
            if cand is None:
                emit("navigator_drop", reason="unknown_target", target=plan.target)
                continue
            queries: list[str] = []
            for q in plan.queries[:queries_per_node]:
                q = q.strip()
                if not any(noun in q for noun in cand.proper_nouns):
                    emit("navigator_fix", reason="no_proper_noun", query=q, target=plan.target)
                    q = next((t for t in cand.templates
                              if normalize_text(t) not in seen_norm), "")
                if not q:
                    continue
                norm = normalize_text(q)
                if norm in seen_norm:
                    emit("navigator_drop", reason="duplicate", query=q)
                    continue
                seen_norm.add(norm)
                queries.append(q)
            if queries:
                validated.append(QueryPlan(target=plan.target, queries=queries, why=plan.why))
        return validated


def query_hash(q: str) -> str:
    """검색어 → 안정적 query_hash (정규화 후 sha256[:16]). Query 노드 키."""
    return short_hash(normalize_text(q))


def write_query_nodes(
    driver: Driver,
    plans: list[QueryPlan],
    candidates: list[FrontierCandidate],
    *,
    collected_by: str,
) -> dict[str, str]:
    """Query 노드 + (타깃)-[:SPAWNED_QUERY]->(Query) 적재. {query_text: query_hash} 반환."""
    by_ref = {c.ref: c for c in candidates}
    out: dict[str, str] = {}
    rows = []
    for plan in plans:
        cand = by_ref[plan.target]
        for q in plan.queries:
            qh = query_hash(q)
            out[q] = qh
            rows.append({
                "qh": qh, "text": q,
                "executed_at": datetime.now().isoformat(timespec="seconds"),
                "hop_path": cand.ref, "collected_by": collected_by,
                "src_label": cand.label, "src_key": cand.key,
            })
    if not rows:
        return out
    with driver.session() as s:
        for label in {r["src_label"] for r in rows}:
            key_prop = KEY_PROP[label]
            s.run(
                f"""
                UNWIND $rows AS row
                MERGE (q:{L.QUERY} {{query_hash: row.qh}})
                SET q.text = row.text, q.executed_at = row.executed_at,
                    q.hop_path = row.hop_path, q.collected_by = row.collected_by
                WITH q, row
                MATCH (src:{label} {{{key_prop}: row.src_key}})
                MERGE (src)-[:{E.SPAWNED_QUERY}]->(q)
                """,
                rows=[r for r in rows if r["src_label"] == label],
            )
    return out
