"""커버리지 측정 (음악저작권) — 4축 척추 노드의 Fact/출처/검색 커버리지. real_estate 미러.

Moderator가 '어느 축이 비었나(unknown-unknowns)'를 판단하는 정량 신호. 순수 Cypher,
명시 앵커 스코프(Work에서 축별 라벨로 도달). real_estate의 axis_coverage 음악판.

축 척추(영향력/대중성/지속성/사회적 가치)에 닿는 노드별로:
    n_facts(Fact MENTIONS), n_press(distinct WebDoc), n_queries(SPAWNED_QUERY).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from neo4j import Driver

from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.traversal import scope_eids

_AXES = ("influence", "popularity", "sustainability", "social_value")

# 축별 척추 라벨 — Work에서 이 라벨 노드까지가 그 축의 평가 근거.
_AXIS_LABELS: dict[str, list[str]] = {
    "influence": [L.ARTIST, L.AWARD, L.MEDIA_EVENT, L.VIRAL_TREND, L.GENRE],
    "popularity": [L.PLATFORM, L.CHART, L.FANDOM, L.MEDIA_EVENT, L.VIRAL_TREND],
    "sustainability": [L.PLATFORM, L.RIGHTSHOLDER, L.AGENCY, L.RELEASE],
    "social_value": [L.ARTIST, L.SOCIAL_THEME, L.CAUSE, L.PUBLIC_USE, L.FANDOM,
                     L.MEDIA_EVENT, L.VIRAL_TREND],
}


@dataclass
class AxisCoverage:
    axis: str
    n_nodes: int = 0
    n_facts: int = 0
    n_press: int = 0
    uncited: list[str] = field(default_factory=list)     # Fact 0건 노드 카드
    unexplored: list[str] = field(default_factory=list)  # 검색 0건 노드 카드

    @property
    def thinness(self) -> float:
        return len(self.uncited) / self.n_nodes if self.n_nodes else 1.0


def axis_coverage(driver: Driver, work_id: str, *, cap: int = 6) -> dict[str, AxisCoverage]:
    """Measure typed-node facts plus facts collected by axis-tagged research tasks."""
    out: dict[str, AxisCoverage] = {}
    scoped = list(scope_eids(driver, work_id, 4))
    with driver.session() as s:
        for axis, labels in _AXIS_LABELS.items():
            rows = s.run(
                f"""
                UNWIND $eids AS eid
                MATCH (n) WHERE elementId(n)=eid
                  AND any(l IN labels(n) WHERE l IN $labels)
                OPTIONAL MATCH (f:{L.FACT})-[:MENTIONS]->(n)
                OPTIONAL MATCH (f)-[:EXTRACTED_FROM]->(d:{L.WEB_DOC})
                OPTIONAL MATCH (n)-[:SPAWNED_QUERY]->(q:{L.QUERY})
                WHERE q.axis = $axis
                RETURN labels(n)[0] AS label, n.card_text AS card,
                       collect(DISTINCT elementId(f)) AS facts,
                       collect(DISTINCT elementId(d)) AS docs,
                       collect(DISTINCT elementId(q)) AS queries
                """, eids=scoped, labels=labels, axis=axis,
            ).data()
            research = s.run(
                f"""
                MATCH (w:{L.WORK} {{work_id: $wid}})
                      -[:SPAWNED_QUERY]->(q:{L.QUERY})
                WHERE q.axis = $axis
                OPTIONAL MATCH (f:{L.FACT})-[:EXTRACTED_BY]->(q)
                OPTIONAL MATCH (f)-[:EXTRACTED_FROM]->(d:{L.WEB_DOC})
                RETURN collect(DISTINCT elementId(q)) AS queries,
                       collect(DISTINCT elementId(d)) AS docs,
                       collect(DISTINCT elementId(f)) AS facts,
                       w.card_text AS work_card
                """, wid=work_id, axis=axis,
            ).single()
            fact_ids = set(research["facts"] or []) if research else set()
            doc_ids = set(research["docs"] or []) if research else set()
            query_ids = set(research["queries"] or []) if research else set()
            cov = AxisCoverage(axis=axis, n_nodes=1)
            for r in rows:
                cov.n_nodes += 1
                # Artist 같은 공유 typed node가 언급된 모든 Fact를 이 축에 복제하지 않는다.
                # 축별 Query에서 수집된 Fact와의 교집합만 해당 노드의 axis evidence다.
                node_facts = set(r["facts"] or []) & fact_ids
                node_queries = set(r["queries"] or [])
                query_ids.update(node_queries)
                if not node_facts and len(cov.uncited) < cap:
                    cov.uncited.append((r["card"] or r["label"])[:60])
                if not node_queries and len(cov.unexplored) < cap:
                    cov.unexplored.append((r["card"] or r["label"])[:60])
            cov.n_facts = len(fact_ids)
            cov.n_press = len(doc_ids)
            if not research or not research["queries"]:
                card = research["work_card"] if research else "평가 대상 Work"
                cov.unexplored.insert(0, (card or "평가 대상 Work")[:60])
            if not fact_ids:
                card = research["work_card"] if research else "평가 대상 Work"
                cov.uncited.insert(0, (card or "평가 대상 Work")[:60])
            cov.uncited = cov.uncited[:cap]
            cov.unexplored = cov.unexplored[:cap]
            out[axis] = cov
    return out


def axis_fact_samples(driver: Driver, work_id: str, *, per_axis: int = 3) -> dict[str, list[str]]:
    """축별 상위 Fact 텍스트 표본 — Moderator가 수치뿐 아니라 내용을 보게."""
    out: dict[str, list[str]] = {}
    with driver.session() as s:
        for axis in _AXIS_LABELS:
            researched = s.run(
                f"""
                MATCH (w:{L.WORK} {{work_id: $wid}})-[:SPAWNED_QUERY]->(q:{L.QUERY})
                WHERE q.axis = $axis
                MATCH (f:{L.FACT})-[:EXTRACTED_BY]->(q)
                RETURN DISTINCT elementId(f) AS eid, f.text AS t, f.confidence AS c
                """, wid=work_id, axis=axis,
            ).data()
            by_eid = {r["eid"]: r for r in researched if r.get("t")}
            ranked = sorted(
                by_eid.values(), key=lambda r: (-(r.get("c") or 0.0), r["eid"]))
            out[axis] = [r["t"] for r in ranked[:per_axis]]
    return out


def coverage_summary_text(cov: dict[str, AxisCoverage]) -> str:
    """Moderator 프롬프트용 커버리지 요약 텍스트."""
    lines = []
    for axis in _AXES:
        c = cov.get(axis)
        if not c:
            continue
        lines.append(f"[{axis}] 척추노드 {c.n_nodes}개 · Fact {c.n_facts}건 · "
                     f"출처(press) {c.n_press}개 · 미인용 {len(c.uncited)}개")
        if c.uncited:
            lines.append("   · Fact 전무: " + ", ".join(c.uncited[:4]))
        if c.unexplored:
            lines.append("   · 미검색: " + ", ".join(c.unexplored[:4]))
    return "\n".join(lines)
