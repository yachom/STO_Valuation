"""음악저작권 벡터 인덱스 — 인덱싱 대상 라벨(도메인) + 주입 래퍼.

연산 메커니즘은 infra.graph.vector_index에 있다. 여기엔 "어떤 라벨을 인덱싱하나"
(INDEXED_LABELS)만 두고, 그 목록을 주입하는 얇은 래퍼로 노출한다(real_estate와
동일 패턴 — infra 코드 복제 없음).

QA 앵커 매칭(설계 §5.6)이 이 인덱스를 사용한다.
"""

from __future__ import annotations

from neo4j import Driver

from infra.graph.embedder import dim
from infra.graph.vector_index import _index_name  # noqa: F401  (재노출)
from infra.graph.vector_index import (
    search_similar,  # noqa: F401  (인자 동일 — 그대로 재노출)
    update_embeddings_for_label,  # noqa: F401  (인자 동일 — 그대로 재노출)
)
from infra.graph.vector_index import ensure_vector_index as _ensure_vector_index
from infra.graph.vector_index import update_all_embeddings as _update_all_embeddings
from music_copyright.graphflow.graph.schema import NODE_LABELS

# card_text를 갖는, QA 앵커가 될 수 있는 라벨. 노드 없는 라벨의 인덱스는 무해.
INDEXED_LABELS = [
    NODE_LABELS.WORK,
    NODE_LABELS.ARTIST,
    NODE_LABELS.RIGHTSHOLDER,
    NODE_LABELS.PLATFORM,
    NODE_LABELS.CHART,
    NODE_LABELS.AWARD,
    NODE_LABELS.CERTIFICATION,
    NODE_LABELS.SOCIAL_METRIC,
    # 확장 단계에서 채워짐 (web-search-first)
    NODE_LABELS.FACT,
    NODE_LABELS.ENTITY,
    NODE_LABELS.WEB_DOC,
]


def ensure_vector_index(driver: Driver) -> None:
    """라벨별 vector index 생성. idempotent. (INDEXED_LABELS 주입)"""
    return _ensure_vector_index(driver, INDEXED_LABELS, dim=dim())


def update_all_embeddings(driver: Driver, *, overwrite: bool = False) -> dict:
    return _update_all_embeddings(driver, INDEXED_LABELS, overwrite=overwrite)
