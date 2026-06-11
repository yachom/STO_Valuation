"""BaseTool — 모든 graphflow 툴의 얇은 공통 베이스.

목적은 트레이싱/로깅 공통 층의 1회 배선. 개별 툴은 `_run(**kwargs)`만 구현하면
호출 시 자동으로:
    - Langfuse tool span으로 추적되고 (observe),
    - 로컬 사이드카에 emit_tool_call로 풀 페이로드가 기록되며,
    - 타이밍/에러가 캡처된다.

사용:
    class NaverNewsSearch(BaseTool):
        name = "naver_news_search"
        def _run(self, *, query, display=10):
            ...
            return {"query": query, "items": [...]}

    tool = NaverNewsSearch()
    result = tool(query="강남역 카페 출점")     # 추적은 자동
"""

from __future__ import annotations

import logging
import time

from real_estate.graphflow.tracing import emit_tool_call, observe

logger = logging.getLogger("graphflow.tool")


class BaseTool:
    """조회/외부 호출 툴 공통 베이스. 서브클래스는 `name`과 `_run`을 정의한다."""

    name: str = ""
    as_type: str = "tool"

    def __init__(self, name: str | None = None):
        self.name = name or self.name
        if not self.name:
            raise ValueError("BaseTool.name 미설정 — 서브클래스 클래스변수나 인자로 지정")
        self._observed = observe(name=self.name, as_type=self.as_type)

    def __call__(self, **kwargs):
        @self._observed
        def _do():
            return self._run(**kwargs)

        t0 = time.monotonic()
        try:
            result = _do()
            emit_tool_call(
                tool=self.name, status="ok", args=kwargs, result=result,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            return result
        except Exception as exc:
            emit_tool_call(
                tool=self.name, status="error", args=kwargs,
                result={"error": f"{type(exc).__name__}: {exc}"},
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            logger.warning("tool %s 실패: %s", self.name, exc)
            raise

    def _run(self, **kwargs):
        raise NotImplementedError("서브클래스에서 _run(**kwargs) 구현")
