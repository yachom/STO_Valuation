"""그래프 스키마 메커니즘 — 자산 무관 spec + UNIQUE constraint 적용기.

스키마 *내용*(어떤 라벨/엣지/키가 있는가)은 자산 도메인이다. 여기엔 그 내용을
담는 그릇(GraphSchemaSpec)과, spec으로부터 Neo4j UNIQUE constraint를 만들어
적용하는 메커니즘만 둔다. 각 자산은 자신의 NODE_LABELS/EDGE_TYPES/KEY_PROP로
spec을 만들고 `apply_constraints(driver, spec)`를 호출한다.

레퍼런스: docs/design/graph_native_flow.md §4
"""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import Driver


@dataclass(frozen=True)
class GraphSchemaSpec:
    """한 자산의 그래프 스키마 명세.

    node_labels / edge_types : typo 방지용 상수 모음(메커니즘은 직접 안 쓰지만
        자산 패키지가 한 곳에 모아 보유·노출하도록 spec에 담는다).
    key_props : 라벨 → 자연 키 프로퍼티. UNIQUE constraint + 제네릭 writer의
        MERGE 키가 된다.
    """

    node_labels: tuple[str, ...]
    edge_types: tuple[str, ...]
    key_props: dict[str, str]


def _constraint_name(label: str, key: str) -> str:
    return f"uniq_{label.lower()}_{key.lower()}"


def constraint_statements(spec: GraphSchemaSpec) -> list[str]:
    """spec.key_props로부터 CREATE CONSTRAINT cypher 목록 생성 (idempotent)."""
    return [
        f"CREATE CONSTRAINT {_constraint_name(label, key)} IF NOT EXISTS "
        f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE"
        for label, key in spec.key_props.items()
    ]


def apply_constraints(driver: Driver, spec: GraphSchemaSpec) -> None:
    """spec의 모든 UNIQUE constraint 생성. idempotent."""
    with driver.session() as session:
        for cypher in constraint_statements(spec):
            session.run(cypher)
