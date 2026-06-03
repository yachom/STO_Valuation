"""SQLite real_estate.db → Neo4j 전체 적재 sanity check.

전제:
    1. docker-compose up -d neo4j 로 로컬 Neo4j 기동
    2. .env에 NEO4J_URI/USER/PASSWORD 설정
    3. real_estate.db 빌드 완료 (master_db, efficiency_pipeline, gseed_pipeline 등)

사용:
    python -m scripts.try_neo4j_ingest
"""

import json
import logging
import sys
from pathlib import Path

from infra.env import load_env
from real_estate.report.graph.driver import close_neo4j_driver
from real_estate.report.graph.ingest import ingest_all

DB_PATH = Path("real_estate/database/DB/real_estate.db")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    load_env()
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} 가 없습니다. 데이터 빌더들 먼저 실행.")

    try:
        summary = ingest_all(DB_PATH)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        close_neo4j_driver()


if __name__ == "__main__":
    main()
