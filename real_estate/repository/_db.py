"""Repository 공통 — SQLite 연결 컨텍스트매니저 (try/finally close 보일러플레이트 제거)."""
from __future__ import annotations
from contextlib import contextmanager
import sqlite3


@contextmanager
def connection(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
