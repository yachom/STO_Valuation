"""YouTube 추출 계층 예외."""


class YouTubeError(RuntimeError):
    """YouTube 검색/추출 실패 공통 베이스."""

    def __init__(self, message, *, cause=None):
        super().__init__(message)
        self.cause = cause


class YouTubeDependencyError(YouTubeError):
    """yt-dlp 등 optional dependency가 설치되지 않음."""


class YouTubeExtractError(YouTubeError):
    """yt-dlp 검색/추출 실패."""
