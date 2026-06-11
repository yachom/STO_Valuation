"""graphflow 그래프 인프라 — Neo4j driver, 스키마, 카드, 벡터 인덱스.

진실 저장소는 여전히 SQLite(real_estate.db). 그래프엔 entity + 관계 + 수집물만
둔다. 노드 키는 SQLite 자연 키를 그대로 써서 양방향 traceability를 유지한다.
"""

from real_estate.graphflow.graph.driver import close_neo4j_driver, get_neo4j_driver
from real_estate.graphflow.graph.schema import (
    CONSTRAINTS,
    EDGE_TYPES,
    KEY_PROP,
    NODE_LABELS,
    apply_constraints,
)

__all__ = [
    "get_neo4j_driver",
    "close_neo4j_driver",
    "NODE_LABELS",
    "EDGE_TYPES",
    "KEY_PROP",
    "CONSTRAINTS",
    "apply_constraints",
]
