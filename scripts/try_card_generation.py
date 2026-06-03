"""Neo4j 노드 텍스트 카드 일괄 생성 sanity check.

전제: docker-compose up -d neo4j + ingest_all 완료.

사용:
    python -m scripts.try_card_generation [--overwrite]
"""

import argparse
import json
import logging

from infra.env import load_env
from real_estate.report.graph.cards import update_all_cards
from real_estate.report.graph.driver import close_neo4j_driver, get_neo4j_driver


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()

    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true",
                        help="이미 카드가 있는 노드도 재생성")
    args = parser.parse_args()

    driver = get_neo4j_driver()
    try:
        summary = update_all_cards(driver, overwrite=args.overwrite)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    main()
