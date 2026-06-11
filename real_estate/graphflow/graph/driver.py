"""Neo4j driver singleton.

infra/env.py 컨벤션과 동일하게 환경변수에서 인증 정보를 읽어 단일 driver를
재사용한다. neo4j-driver는 driver를 스레드 간 공유해도 안전하다.

env (.env):
    NEO4J_URI       기본 bolt://localhost:7687
    NEO4J_USER      기본 neo4j
    NEO4J_PASSWORD  필수 — docker-compose 기동 시 설정한 비밀번호
"""

from __future__ import annotations

import logging
import os
import threading

from neo4j import Driver, GraphDatabase

logger = logging.getLogger("infra.neo4j")

# 빈 라벨/프로퍼티 첫 조회 시 서버가 보내는 'does not exist' 알림은 정상 흐름
# (수집물 라벨은 점진 생성됨) — 로그 소음만 만드므로 억제한다.
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

_driver_lock = threading.Lock()
_driver: Driver | None = None


def get_neo4j_driver() -> Driver:
    global _driver
    if _driver is not None:
        return _driver
    with _driver_lock:
        if _driver is not None:
            return _driver
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        password = os.environ.get("NEO4J_PASSWORD", "")
        if not password:
            raise RuntimeError(
                "NEO4J_PASSWORD env가 비어 있습니다. "
                "docker-compose up -d neo4j 후 .env에 비밀번호를 넣으세요."
            )
        _driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info("Neo4j driver 생성 — %s", uri)
    return _driver


def close_neo4j_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
        logger.info("Neo4j driver 종료")
