from infra.repository.external.naver_news_scraper.exceptions import NewsScrapeError
from infra.repository.external.naver_news_scraper.scraper import (
    NewsArticle,
    fetch_news_article,
)

__all__ = ["NewsScrapeError", "NewsArticle", "fetch_news_article"]
