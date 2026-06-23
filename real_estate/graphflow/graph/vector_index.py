"""부동산 벡터 인덱스 — 인덱싱 대상 라벨(도메인) + 주입 래퍼.

연산 메커니즘은 infra.graph.vector_index에 있다. 여기엔 "어떤 라벨을 인덱싱하나"
(INDEXED_LABELS)만 두고, 그 목록을 주입하는 얇은 래퍼로 노출한다 — 기존 호출부
(`ensure_vector_index(driver)` / `update_all_embeddings(driver)`)는 변경 없음.

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
from real_estate.graphflow.graph.schema import NODE_LABELS

# card_text를 갖는, QA 앵커가 될 수 있는 라벨. 노드가 없는 라벨의 인덱스는 무해.
INDEXED_LABELS = [
    NODE_LABELS.ASSET,
    NODE_LABELS.COMPLEX,
    NODE_LABELS.BUILDING,
    NODE_LABELS.CERTIFICATION,
    NODE_LABELS.COMMERCIAL_AREA,
    NODE_LABELS.INDUSTRY,
    NODE_LABELS.ADMIN_DONG,
    NODE_LABELS.LEGAL_DONG,
    NODE_LABELS.TRANSIT_STATION,
    NODE_LABELS.DEVELOPMENT_PROJECT,
    NODE_LABELS.TENANCY_EVENT,
    NODE_LABELS.TENANT,           # 입점업체 — 의미검색/하이브리드 retrieval에 필수
    NODE_LABELS.MARKET_CONTEXT,   # REB 임대시장 환경
    NODE_LABELS.TURNOVER_STAT,    # 건물 임차 회전율
    # 확장/보고서 단계에서 채워짐 (PR-C~)
    NODE_LABELS.FACT,
    NODE_LABELS.ENTITY,
    NODE_LABELS.WEB_DOC,
    NODE_LABELS.USER_DOC,    # 발행자 첨부 자료 — 증거로 인용되므로 검색 대상
    NODE_LABELS.CLAIM,
    NODE_LABELS.REPORT_SECTION,
]


def ensure_vector_index(driver: Driver) -> None:
    """라벨별 vector index 생성. idempotent. (INDEXED_LABELS 주입)"""
    return _ensure_vector_index(driver, INDEXED_LABELS, dim=dim())


def update_all_embeddings(driver: Driver, *, overwrite: bool = False) -> dict:
    return _update_all_embeddings(driver, INDEXED_LABELS, overwrite=overwrite)
