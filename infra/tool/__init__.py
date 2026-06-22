"""infra/tool — 자산 공통 도구 (여러 기초자산에서 재사용).

BaseTool: 모든 도구의 트레이싱 래핑 공통 베이스.
naver: 네이버 뉴스 검색/스크랩 — 부동산·음악저작권 등 자산 무관.

자산 전용 도구(예: 부동산 건축HUB 대장)는 <asset>/graphflow/tool/에 둔다.
각 자산은 두 곳(infra/tool 공통 + <asset>/.../tool 전용)에서 도구를 import한다.
"""

from infra.tool.base import BaseTool
from infra.tool.naver import (
    NaverNewsScrapeTool,
    NaverNewsSearchTool,
    NaverVerticalSearchTool,
)

__all__ = [
    "BaseTool",
    "NaverNewsSearchTool",
    "NaverNewsScrapeTool",
    "NaverVerticalSearchTool",
]
