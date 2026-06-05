"""SQL 자동 트레이싱 — sqlite3.Connection 래퍼.

`DataScout._conn()`이나 `SqliteRetriever.__init__`처럼 SQLite를 직접 여는 곳에서
`sqlite3.connect(path)` 대신 `tracing_connect(path, source=...)`를 호출하면,
`execute()` 한 번당 `sql_query` 이벤트와 사이드카 페이로드가 자동 기록된다.

이벤트 필드:
    source       — "data_scout" / "sqlite_retriever:tenant" 등 호출자 식별
    sql          — SQL 첫 300자
    params_count — 바인딩 인자 수 (정확한 값은 payload에)
    row_count    — fetchall 결과 행 수
    duration_ms
    payload_path — sql_calls/{call_id}.json (풀 SQL + 풀 params)

설계 노트:
    execute() 시점에 fetchall()을 한 번 하고 결과를 _ReplayCursor로 감싸 호출자가
    fetchone()/fetchall()을 그대로 쓸 수 있게 한다. 기존 코드는 모두 execute() 후
    바로 fetchone()/fetchall()을 부르므로 이중 fetch가 발생하지 않는다.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from real_estate.report.tracing import emit, get_tracer


def tracing_connect(db_path: str, *, source: str) -> sqlite3.Connection:
    """sqlite3.connect 대체. 같은 인터페이스 + 자동 트레이싱."""
    inner = sqlite3.connect(db_path)
    inner.row_factory = sqlite3.Row
    return _TracingConnection(inner, source=source)  # type: ignore[return-value]


class _ReplayCursor:
    """fetchall된 rows를 fetchone/fetchall로 재공급."""

    def __init__(self, rows: list, description: Any):
        self._rows = list(rows)
        self._idx = 0
        self.description = description

    def fetchone(self):
        if self._idx < len(self._rows):
            r = self._rows[self._idx]
            self._idx += 1
            return r
        return None

    def fetchall(self):
        out = self._rows[self._idx:]
        self._idx = len(self._rows)
        return out

    def __iter__(self):
        while self._idx < len(self._rows):
            yield self._rows[self._idx]
            self._idx += 1


class _TracingConnection:
    """sqlite3.Connection 위임 + execute 트레이싱."""

    def __init__(self, inner: sqlite3.Connection, source: str):
        self._inner = inner
        self._source = source

    def execute(self, sql: str, params: Any = ()) -> _ReplayCursor:
        t0 = time.monotonic()
        try:
            cur = self._inner.execute(sql, params)
            rows = cur.fetchall()
            description = cur.description
        except Exception as exc:
            emit("error", phase="sql", source=self._source,
                 sql=sql[:300], error=type(exc).__name__, message=str(exc)[:300])
            raise
        duration_ms = int((time.monotonic() - t0) * 1000)
        tracer = get_tracer()
        payload_path = None
        payload_sha = None
        if tracer is not None:
            call_id = tracer.next_call_id()
            try:
                params_list = list(params) if not isinstance(params, (str, bytes)) else [params]
            except TypeError:
                params_list = [params]
            payload_path, payload_sha = tracer.write_payload(
                "sql_calls", call_id,
                {"source": self._source, "sql": sql,
                 "params": [str(p) for p in params_list],
                 "row_count": len(rows), "duration_ms": duration_ms},
            )
        emit("sql_query", source=self._source,
             sql=sql[:300], params_count=len(params) if hasattr(params, "__len__") else 0,
             row_count=len(rows), duration_ms=duration_ms,
             payload_path=payload_path, payload_sha=payload_sha)
        return _ReplayCursor(rows, description)

    # 위임 메서드들
    def cursor(self):
        return self._inner.cursor()

    def commit(self):
        return self._inner.commit()

    def rollback(self):
        return self._inner.rollback()

    def close(self):
        return self._inner.close()

    @property
    def row_factory(self):
        return self._inner.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._inner.row_factory = value

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._inner.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        # 그 외 sqlite3.Connection 속성/메서드 전부 위임
        return getattr(self._inner, name)
