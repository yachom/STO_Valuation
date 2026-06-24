"""커버리지 측정 (음악저작권) — 3축 척추 노드의 Fact/출처/검색 커버리지. real_estate 미러.

Moderator가 '어느 축이 비었나(unknown-unknowns)'를 판단하는 정량 신호. 순수 Cypher,
명시 앵커 스코프(Work에서 축별 라벨로 도달). real_estate의 axis_coverage 음악판.

축 척추(영향력/대중성/지속성)에 닿는 노드별로:
    n_facts(Fact MENTIONS), n_press(distinct WebDoc), n_queries(SPAWNED_QUERY).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from neo4j import Driver

from music_copyright.graphflow.graph.schema import NODE_LABELS as L

_AXES = ("influence", "popularity", "sustainability")

# 축별 척추 라벨 — Work에서 이 라벨 노드까지가 그 축의 평가 근거.
_AXIS_LABELS: dict[str, list[str]] = {
    "influence": [L.ARTIST, L.AWARD],
    "popularity": [L.PLATFORM, L.CHART],
    "sustainability": [L.PLATFORM, L.RIGHTSHOLDER],
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
    """축별 커버리지 측정. Work에서 축 라벨 노드까지 스코프 한정."""
    out: dict[str, AxisCoverage] = {}
    with driver.session() as s:
        for axis, labels in _AXIS_LABELS.items():
            rows = s.run(
                f"""
                MATCH (w:{L.WORK} {{work_id: $wid}})
                MATCH (w)-[*0..3]-(n)
                WHERE n = w OR any(l IN labels(n) WHERE l IN $labels)
                WITH DISTINCT n
                OPTIONAL MATCH (f:{L.FACT})-[:MENTIONS]->(n)
                OPTIONAL MATCH (f)-[:EXTRACTED_FROM]->(d:{L.WEB_DOC})
                OPTIONAL MATCH (n)-[:SPAWNED_QUERY]->(q:{L.QUERY})
                RETURN labels(n)[0] AS label, n.card_text AS card,
                       count(DISTINCT f) AS nf, count(DISTINCT d) AS npress,
                       count(DISTINCT q) AS nq
                """, wid=work_id, labels=labels,
            ).data()
            cov = AxisCoverage(axis=axis)
            for r in rows:
                cov.n_nodes += 1
                cov.n_facts += r["nf"]
                cov.n_press += r["npress"]
                if r["nf"] == 0 and len(cov.uncited) < cap:
                    cov.uncited.append((r["card"] or r["label"])[:60])
                if r["nq"] == 0 and len(cov.unexplored) < cap:
                    cov.unexplored.append((r["card"] or r["label"])[:60])
            out[axis] = cov
    return out


def axis_fact_samples(driver: Driver, work_id: str, *, per_axis: int = 3) -> dict[str, list[str]]:
    """축별 상위 Fact 텍스트 표본 — Moderator가 수치뿐 아니라 내용을 보게."""
    out: dict[str, list[str]] = {}
    with driver.session() as s:
        for axis, labels in _AXIS_LABELS.items():
            rows = s.run(
                f"""
                MATCH (w:{L.WORK} {{work_id: $wid}})-[*0..3]-(n)
                WHERE any(l IN labels(n) WHERE l IN $labels)
                MATCH (f:{L.FACT})-[:MENTIONS]->(n)
                RETURN DISTINCT f.text AS t, f.confidence AS c
                ORDER BY c DESC LIMIT $k
                """, wid=work_id, labels=labels, k=per_axis,
            ).data()
            out[axis] = [r["t"] for r in rows if r["t"]]
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
