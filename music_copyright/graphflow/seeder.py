"""Seeder (음악저작권) — STUB. 웹검색 우선 시드 (web-search-first).

real_estate의 Seeder는 SQLite 구조 데이터를 그래프로 스코프 적재한다. 음악 도메인은
구조화 데이터가 희소하므로, 시드 자체가 **웹검색 주도**다: 작품 1건(work_id) →
ddgs/naver/tavily로 아티스트·권리자·플랫폼·차트·수상 후보를 찾아 Work 앵커에 붙인다.

⚠️ 이 파일은 STUB이다 — 실제 네트워크 호출/파이프라인은 구현하지 않는다.
collect_scope는 빈 ScopeGraph를, seed는 Work 앵커 1노드만 MERGE하는 최소 골격이다.
real_estate/graphflow/seeder.py의 2단 구조(collect_scope → seed)를 미러링해, 도메인
확정 시 채울 자리를 명시한다.

TODO(domain):
  - collect_scope: infra.tool.ddgs/naver/tavily로 work 메타 + 관계 후보 수집,
    Fact/Entity/WebDoc 추출까지(또는 expand로 위임).
  - seed: 수집된 노드/엣지를 KEY_PROP MERGE로 적재 + 카드 임베딩.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from neo4j import Driver

from music_copyright.graphflow.graph.cards import build_card
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L

logger = logging.getLogger("music_copyright.seeder")


# ───────────────────────────────────────────────────────────
# 스코프 그래프 (직렬화 가능한 중간 표현) — real_estate와 동형
# ───────────────────────────────────────────────────────────
@dataclass
class SeedNode:
    label: str
    key: str                       # 키 프로퍼티 값
    props: dict = field(default_factory=dict)
    card_text: str = ""


@dataclass
class SeedEdge:
    src_label: str
    src_key: str
    rel: str
    dst_label: str
    dst_key: str
    props: dict = field(default_factory=dict)


@dataclass
class ScopeGraph:
    nodes: list[SeedNode] = field(default_factory=list)
    edges: list[SeedEdge] = field(default_factory=list)


# ───────────────────────────────────────────────────────────
# 1단계: 오프라인 수집 (web-search-first) — STUB
# ───────────────────────────────────────────────────────────
def collect_scope(work_id: str, *, title: str | None = None) -> ScopeGraph:
    """work_id → 자산 스코프 그래프 (STUB).

    지금은 Work 앵커 1노드만 담는다. 도메인 확정 시 여기서 웹검색
    (infra.tool.ddgs/naver/tavily)으로 아티스트·권리자·플랫폼·차트·수상 후보와
    그 엣지를 채운다. 결정적·idempotent를 유지할 것.
    """
    props = {"work_id": work_id, "title": title}
    card = build_card(L.WORK, props)
    work = SeedNode(label=L.WORK, key=work_id, props=props, card_text=card)
    logger.info("collect_scope(STUB) work=%s → 1 anchor node", work_id)
    # TODO(domain): 웹검색 수집 + 관계 후보 노드/엣지 추가.
    return ScopeGraph(nodes=[work], edges=[])


# ───────────────────────────────────────────────────────────
# 2단계: Neo4j 적재 — 최소 골격(앵커 MERGE만)
# ───────────────────────────────────────────────────────────
def seed(scope: ScopeGraph, driver: Driver) -> dict:
    """ScopeGraph를 Neo4j에 MERGE 적재 (STUB — 노드만, 카드 임베딩 생략).

    KEY_PROP를 MERGE 키로 쓴다(infra writer 규약과 동일). 엣지 적재/임베딩은
    도메인 확정 시 채운다. real_estate.seed의 형태만 미러링한 골격이다.
    """
    counts = {"nodes": 0, "edges": 0}
    with driver.session() as s:
        for n in scope.nodes:
            key_prop = KEY_PROP.get(n.label)
            if not key_prop:
                logger.warning("KEY_PROP 미정의 라벨 skip: %s", n.label)
                continue
            s.run(
                f"MERGE (x:{n.label} {{{key_prop}: $key}}) "
                "SET x += $props, x.card_text = $card",
                key=n.key, props={k: v for k, v in n.props.items() if v is not None},
                card=n.card_text,
            )
            counts["nodes"] += 1
        # TODO(domain): 엣지 MERGE (src/dst KEY_PROP 매칭) + 카드 임베딩 채우기.
    logger.info("seed(STUB) → %s", counts)
    return counts
