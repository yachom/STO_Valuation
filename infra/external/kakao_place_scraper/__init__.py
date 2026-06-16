"""Kakao 장소 상세 리뷰 스크래퍼 — 평점·후기수 (헤드리스 브라우저).

카카오 공식 Local API는 리뷰/평점을 제공하지 않는다. place.map.kakao.com 상세
페이지가 내부 호출하는 place-api panel3 응답(JSON)에 카카오맵 평점·후기수·블로그
리뷰수가 들어 있어, 이를 Playwright로 페이지를 띄워 가로챈다(직접 호출은 406).

로거: external.kakao_place (cf. infra/external/naver_news_scraper).
"""

from infra.external.kakao_place_scraper.scraper import (
    KakaoPlaceScraper,
    PlaceReview,
)

__all__ = ["KakaoPlaceScraper", "PlaceReview"]
