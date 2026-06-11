"""graphflow 툴 — 네이버 검색/스크랩, 공공 API 재조회, walk 등 외부/조회 도구.

모두 BaseTool을 상속해 호출 시 자동으로 (a) Langfuse tool span (b) 로컬 사이드카
emit_tool_call (c) 타이밍/에러 캡처를 얻는다. 툴 코드는 트레이싱을 직접 짜지 않는다.
"""

from real_estate.graphflow.tool.base import BaseTool

__all__ = ["BaseTool"]
