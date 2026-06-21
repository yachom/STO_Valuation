"""커버리지 메트릭 — 그래프를 Co-STORM 마인드맵으로 보고 '어디가 비었나'를 측정 (D19).

Moderator가 unknown-unknowns를 사냥하려면 빈 곳을 **측정 가능**하게 만들어야 한다.
이 모듈은 자산의 3축(tenant/esg/commercial) **구조 척추**를 잡아, 축별:
  - Fact 밀도        — 구조 노드를 가리키는 Fact 수
  - corroboration    — 그 Fact들을 받치는 distinct press(WebDoc) 수
  - 미인용 노드      — 어떤 Fact도 안 가리키는 구조 노드 (서사 공백)
  - 미검색 노드      — 검색어를 한 번도 안 띄운 노드
  - 신선도 공백      — 만료/노후 Fact 수
를 **순수 Cypher**로 산출한다. 전부 읽기 전용 — 그래프를 바꾸지 않는다.

스코프는 자산의 **고정 앵커 엣지**로 정의한다(D18: 3축 = 구조화 데이터 1:1 매핑).
임의 N홉이 아니라 명시 경로를 쓰는 이유: 전역 공유 그래프에서 N홉은 이웃 자산의
사적 노드(그들의 Tenant/Building)까지 빨아들여 커버리지 신호를 오염시킨다. 앵커
엣지만 따라가면 '이 자산의 척추'만 정확히 잡힌다.

freshness는 validity.py 반감기(Fact 6개월)의 Cypher 근사 — 보고서 경로의 정밀
validity와 별개의 방향성 신호다(질문 주입용 휴리스틱이지 등급 산정 아님).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from neo4j import Driver

from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from infra.agent.tracing import add_score

logger = logging.getLogger("graphflow.coverage")

_AXES = ("tenant", "esg", "commercial")     # 고정 순서 (재현성)

# 축 = 자산에서 출발하는 고정 앵커 경로(꼬리). 각 패턴의 타깃은 (n:라벨).
# D18: 구조화 소스 1:1 — tenant←HAS_TENANT/TURNOVER/MARKET, esg←ANCHORS Building/CERTIFIED,
# commercial←ANCHORS CommercialArea(+업종·행정동) / WITHIN_RADIUS 배후단지·역.
_AXIS_PATTERNS: dict[str, list[str]] = {
    "tenant": [
        f"-[:{E.HAS_TENANT}]->(n:{L.TENANT})",
        f"-[:{E.HAS_TURNOVER}]->(n:{L.TURNOVER_STAT})",
        f"-[:{E.HAS_MARKET}]->(n:{L.MARKET_CONTEXT})",
    ],
    "esg": [
        f"-[:{E.ANCHORS}]->(n:{L.BUILDING})",
        f"-[:{E.ANCHORS}]->(:{L.BUILDING})-[:{E.CERTIFIED_AS}]->(n:{L.CERTIFICATION})",
    ],
    "commercial": [
        f"-[:{E.ANCHORS}]->(n:{L.COMMERCIAL_AREA})",
        f"-[:{E.ANCHORS}]->(:{L.COMMERCIAL_AREA})-[:{E.TOP_INDUSTRY}]->(n:{L.INDUSTRY})",
        f"-[:{E.ANCHORS}]->(:{L.COMMERCIAL_AREA})-[:{E.SERVES_DISTRICT}]->(n:{L.ADMIN_DONG})",
        f"-[:{E.ANCHORS}]->(n:{L.ADMIN_DONG})",
        f"-[:{E.WITHIN_RADIUS}]->(n:{L.COMPLEX})",
        f"-[:{E.WITHIN_RADIUS}]->(n:{L.TRANSIT_STATION})",
    ],
}

# Fact 신선도 근사 — validity.py의 Fact 반감기(6개월) 경계. 이보다 오래됐고 명시
# 유효기간/정적 표시가 없으면 '신선도 공백'으로 센다(방향성 휴리스틱, 등급용 아님).
_STALE_DAYS = 183

# 라벨별 사람이 읽는 이름 (Tenant=상호, MarketContext=building_type, Certification=kind,
# TurnoverStat=도로명주소 …). 못 찾으면 라벨명으로 폴백.
_NAME = (
    "coalesce(n.trdar_name, n.adstrd_name, n.industry_name, n.상호, "
    "n.building_name, n.bname, n.complex_name, n.station_name, n.kind, "
    "n.building_type, n.도로명주소, n.canonical_name, n.name, labels(n)[0])"
)

# 스코프 UNION (자산 앵커 경로 → (axis, n)). 동일 (axis,n)은 UNION이 dedup.
_SCOPE_UNION = "\n      UNION\n      ".join(
    f"MATCH (a:{L.ASSET} {{asset_id: $aid}}){tail} RETURN '{axis}' AS axis, n"
    for axis in _AXES for tail in _AXIS_PATTERNS[axis]
)

_COVERAGE_CYPHER = f"""
CALL {{
      {_SCOPE_UNION}
}}
WITH DISTINCT axis, n
RETURN axis,
       labels(n)[0] AS label,
       {_NAME} AS name,
       COUNT {{ MATCH (f:{L.FACT})-[:{E.MENTIONS}]->(n) }} AS n_facts,
       COUNT {{ MATCH (f:{L.FACT})-[:{E.MENTIONS}]->(n)
                MATCH (f)-[:{E.EXTRACTED_FROM}]->(d:{L.WEB_DOC})
                RETURN DISTINCT d }} AS n_press,
       COUNT {{ MATCH (n)-[:{E.SPAWNED_QUERY}]->(:{L.QUERY}) }} AS n_queries,
       COUNT {{ MATCH (f:{L.FACT})-[:{E.MENTIONS}]->(n)
                WHERE (f.valid_until IS NOT NULL AND f.valid_until < $today)
                   OR (f.valid_until IS NULL
                       AND coalesce(f.temporal_kind, '') <> 'static'
                       AND f.published_at IS NOT NULL AND f.published_at <> ''
                       AND f.published_at < $stale_cutoff) }} AS n_stale
ORDER BY axis, label, name
"""


@dataclass
class NodeCoverage:
    axis: str
    label: str
    name: str
    n_facts: int
    n_press: int
    n_queries: int
    n_stale: int


@dataclass
class AxisCoverage:
    axis: str
    n_nodes: int = 0
    n_facts: int = 0
    n_press: int = 0
    n_stale: int = 0
    uncited: list[str] = field(default_factory=list)      # Fact 0건 구조노드명
    unexplored: list[str] = field(default_factory=list)   # 검색 0회 구조노드명

    @property
    def thinness(self) -> float:
        """축 구조노드 중 Fact가 하나도 없는 비율 (1.0 = 완전 공백)."""
        return len(self.uncited) / self.n_nodes if self.n_nodes else 1.0

    @property
    def stale_ratio(self) -> float:
        return self.n_stale / self.n_facts if self.n_facts else 0.0


def scope_node_coverage(
    driver: Driver, asset_id: str, *, today: date | None = None,
) -> list[NodeCoverage]:
    """자산 척추 구조노드별 커버리지 — Fact/press/검색/노후 수 (1 쿼리, 읽기전용)."""
    today = today or date.today()
    stale_cutoff = (today - timedelta(days=_STALE_DAYS)).isoformat()
    out: list[NodeCoverage] = []
    with driver.session() as s:
        for r in s.run(_COVERAGE_CYPHER, aid=asset_id,
                       today=today.isoformat(), stale_cutoff=stale_cutoff):
            out.append(NodeCoverage(
                axis=r["axis"], label=r["label"], name=str(r["name"]),
                n_facts=r["n_facts"], n_press=r["n_press"],
                n_queries=r["n_queries"], n_stale=r["n_stale"]))
    return out


def axis_coverage(
    driver: Driver, asset_id: str, *, today: date | None = None, cap: int = 8,
) -> dict[str, AxisCoverage]:
    """노드 커버리지를 3축으로 집계. uncited/unexplored 이름은 cap개까지."""
    cov = {axis: AxisCoverage(axis=axis) for axis in _AXES}
    for nc in scope_node_coverage(driver, asset_id, today=today):
        ac = cov.get(nc.axis)
        if ac is None:
            continue
        ac.n_nodes += 1
        ac.n_facts += nc.n_facts
        ac.n_press += nc.n_press
        ac.n_stale += nc.n_stale
        if nc.n_facts == 0 and len(ac.uncited) < cap:
            ac.uncited.append(nc.name)
        if nc.n_queries == 0 and len(ac.unexplored) < cap:
            ac.unexplored.append(nc.name)
    return cov


def coverage_summary_text(cov: dict[str, AxisCoverage]) -> str:
    """Moderator 프롬프트용 축별 커버리지 요약."""
    lines: list[str] = []
    for axis in _AXES:                  # 고정 순서 (재현성)
        ac = cov.get(axis) or AxisCoverage(axis=axis)
        lines.append(
            f"[{axis}] 구조노드 {ac.n_nodes}개 · Fact {ac.n_facts}건 · "
            f"근거출처(press) {ac.n_press}개 · 미인용 {len(ac.uncited)}개 · "
            f"노후Fact {ac.n_stale}건")
        if ac.uncited:
            lines.append(f"   · Fact 전무(미인용): {', '.join(ac.uncited)}")
        if ac.unexplored:
            lines.append(f"   · 미검색: {', '.join(ac.unexplored)}")
    return "\n".join(lines)


_FACT_SAMPLE_CYPHER = "CALL {\n      " + "\n      UNION\n      ".join(
    f"MATCH (a:{L.ASSET} {{asset_id: $aid}}){tail}<-[:{E.MENTIONS}]-(f:{L.FACT}) "
    f"RETURN '{axis}' AS axis, f.text AS text, coalesce(f.confidence, 0.5) AS conf"
    for axis in _AXES for tail in _AXIS_PATTERNS[axis]
) + "\n}\nWITH DISTINCT axis, text, conf WHERE text IS NOT NULL\nRETURN axis, text ORDER BY axis, conf DESC"


def axis_fact_samples(driver: Driver, asset_id: str, *, per_axis: int = 3) -> dict[str, list[str]]:
    """축별 수집 Fact 텍스트 표본 (confidence 높은 순) — moderator가 카운트만이 아니라
    **실제 마인드맵 내용을 읽고** unknown-unknown 질문을 던지게 한다 (Co-STORM ⑧)."""
    out: dict[str, list[str]] = {ax: [] for ax in _AXES}
    with driver.session() as s:
        for r in s.run(_FACT_SAMPLE_CYPHER, aid=asset_id):
            ax = r["axis"]
            if ax in out and len(out[ax]) < per_axis:
                out[ax].append(r["text"])
    return out


def emit_coverage_scores(cov: dict[str, AxisCoverage]) -> None:
    """Langfuse score 적재 — 커버리지 품질 시계열 (PR-1). 트레이서 없으면 무음."""
    for axis, ac in cov.items():
        add_score(f"coverage_{axis}_facts", ac.n_facts)
        add_score(f"coverage_{axis}_press", ac.n_press)
        add_score(f"coverage_{axis}_thinness", round(ac.thinness, 3))
        add_score(f"coverage_{axis}_stale", ac.n_stale)
