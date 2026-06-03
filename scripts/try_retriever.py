"""Retriever sanity — 카드 임베딩 → vector index 생성 → 샘플 질의.

전제: Neo4j 기동 + ingest_all + update_all_cards 완료.

사용:
    python -m scripts.try_retriever "강남구 OO 아파트 ESG 등급"
"""

import sys

from infra.env import load_env
from real_estate.report.graph.driver import close_neo4j_driver, get_neo4j_driver
from real_estate.report.rag.naive_retriever import NaiveNeighborhoodRetriever
from real_estate.report.rag.vector_index import (
    ensure_vector_index,
    update_all_embeddings,
)


def main() -> None:
    load_env()
    query = " ".join(sys.argv[1:]) or "강남구 아파트 ESG 친환경 인증 등급"

    driver = get_neo4j_driver()
    try:
        ensure_vector_index(driver)
        summary = update_all_embeddings(driver)
        print(f"embed summary: {summary}")

        retriever = NaiveNeighborhoodRetriever(driver)
        for axis in ("tenant", "esg", "commercial"):
            print(f"\n=== axis={axis} ===")
            snippets = retriever.retrieve(query, k=3, axis=axis)
            for i, s in enumerate(snippets):
                print(f"[{i+1}] score={s.score:.3f} labels={s.metadata.get('primary_labels')}")
                print(s.text[:200])
                print(f"  refs={s.node_refs[:3]}... (총 {len(s.node_refs)}개)")
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    main()
