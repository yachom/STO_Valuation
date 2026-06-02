"""네이버 뉴스 HTML 스크래핑 관련 예외."""


class NewsScrapeError(Exception):
    """네이버 뉴스 본문 크롤링 실패."""

    def __init__(self, message, *, url=None, status=None):
        super().__init__(message)
        self.url = url
        self.status = status
