"""그래프 read 헬퍼 — 자주 쓰는 named Cypher 조회를 한 곳에.

`asset_card` 조회(동일 `MATCH (a:Asset {asset_id})`)는 report·qa·moderator·
discourse·expand 5곳에 복붙돼 있었다. 여기로 통합해 스키마/카드 정책이 바뀌어도
한 곳만 고치면 되게 한다. graph/ 서브패키지의 다른 모듈과 동일하게 의존성은
neo4j Driver + schema 라벨뿐(경량 leaf — 무거운 graphflow 모듈을 끌어오지 않음).
"""

from __future__ import annotations

from neo4j import Driver

from real_estate.graphflow.graph.schema import NODE_LABELS as L


def asset_card(driver: Driver, asset_id: str, *, required: bool = False) -> str:
    """Asset 노드의 card_text. 없을 때: required면 ValueError, 아니면 asset_id 폴백.

    - report 등 시드 선행이 보장돼야 하는 곳: ``required=True`` (없으면 즉시 실패).
    - qa/moderator/discourse 등 폴백 허용: ``required=False`` (기본).
    """
    with driver.session() as s:
        r = s.run(
            f"MATCH (a:{L.ASSET} {{asset_id: $aid}}) RETURN a.card_text AS c",
            aid=asset_id,
        ).single()
    if r is None:
        if required:
            raise ValueError(f"Asset 노드 없음: {asset_id}")
        return asset_id
    return r["c"] or asset_id


def asset_card_and_sigungu(driver: Driver, asset_id: str) -> tuple[str, str | None]:
    """Asset의 ``(card_text, sigungu)``. EXPAND가 검색어 지역 힌트로 sigungu를 함께 쓴다.

    Asset 노드가 없으면 ValueError(Seeder 선행 필요).
    """
    with driver.session() as s:
        r = s.run(
            f"MATCH (a:{L.ASSET} {{asset_id: $aid}}) "
            f"RETURN a.card_text AS card, a.sigungu AS sigungu",
            aid=asset_id,
        ).single()
    if r is None:
        raise ValueError(f"Asset 노드 없음: {asset_id} — Seeder 먼저 실행")
    return r["card"] or asset_id, r["sigungu"]
