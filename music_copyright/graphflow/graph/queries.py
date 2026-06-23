"""그래프 조회 (음악저작권) — real_estate/graphflow/graph/queries.py 평행.

보고서/QA가 쓰는 읽기 전용 Cypher 조회를 한 곳에. real_estate의 asset_card에
대응하는 work_card_text 등.
"""

from __future__ import annotations

from neo4j import Driver


def work_card_text(driver: Driver, work_id: str) -> str:
    """Work 카드 텍스트 (시드 선행 필수 — 없으면 ValueError).

    real_estate.graphflow.graph.queries.asset_card에 대응.
    """
    with driver.session() as s:
        r = s.run("MATCH (w:Work {work_id: $wid}) RETURN w.card_text AS c",
                  wid=work_id).single()
    if not r or not r["c"]:
        raise ValueError(f"Work 카드 없음(시드 선행 필요): {work_id}")
    return r["c"]
