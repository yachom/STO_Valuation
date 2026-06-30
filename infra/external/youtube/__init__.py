"""YouTube public search/extract client built on yt-dlp."""

from infra.external.youtube.client import YouTubeClient, YouTubeSearchResult
from infra.external.youtube.exceptions import (
    YouTubeDependencyError,
    YouTubeError,
    YouTubeExtractError,
)

__all__ = [
    "YouTubeClient",
    "YouTubeSearchResult",
    "YouTubeError",
    "YouTubeDependencyError",
    "YouTubeExtractError",
]
