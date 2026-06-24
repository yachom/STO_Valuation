"""음악저작권 전용 Neo4j driver — 부동산과 물리적으로 분리된 인스턴스.

부동산은 infra.graph.driver(get_neo4j_driver, NEO4J_*, 포트 7687)를 쓰고, 음악은
별도 컨테이너(docker-compose neo4j-music, 포트 7688)를 MUSIC_NEO4J_*로 가리킨다.
두 자산의 그래프가 물리적으로 격리된다(이전엔 한 인스턴스를 스코프로만 분리).

infra.graph.driver와 동일 패턴(싱글톤)이지만 별도 인스턴스/별도 env다 — 메커니즘만
같고 대상이 다르다.

env (.env):
    MUSIC_NEO4J_URI       기본 bolt://localhost:7688
    MUSIC_NEO4J_USER      기본 neo4j
    MUSIC_NEO4J_PASSWORD  필수 — docker compose up -d neo4j-music 후 설정
"""

from __future__ import annotations

import logging
import os
import threading

from neo4j import Driver, GraphDatabase

logger = logging.getLogger("music_copyright.driver")
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

_lock = threading.Lock()
_driver: Driver | None = None


def get_music_driver() -> Driver:
    """음악 전용 Neo4j driver 싱글톤 (MUSIC_NEO4J_*)."""
    global _driver
    if _driver is not None:
        return _driver
    with _lock:
        if _driver is not None:
            return _driver
        uri = os.environ.get("MUSIC_NEO4J_URI", "bolt://localhost:7688")
        user = os.environ.get("MUSIC_NEO4J_USER", "neo4j")
        password = os.environ.get("MUSIC_NEO4J_PASSWORD", "")
        if not password:
            raise RuntimeError(
                "MUSIC_NEO4J_PASSWORD env가 비어 있습니다. "
                "docker compose up -d neo4j-music 후 .env에 비밀번호를 넣으세요."
            )
        _driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info("음악 Neo4j driver 생성 — %s", uri)
    return _driver


def close_music_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
        logger.info("음악 Neo4j driver 종료")
