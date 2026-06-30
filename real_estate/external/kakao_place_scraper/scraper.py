"""Kakao 장소 상세 리뷰 스크래퍼 (Playwright).

place.map.kakao.com/{id} 를 헤드리스 크로미움으로 열고, 페이지가 부르는
place-api panel3 응답을 가로채 카카오맵 평점·후기수·블로그리뷰수를 뽑는다.
직접 panel3 호출은 406이라(브라우저 헤더/세션 필요) 페이지를 거친다.

여러 place_id를 한 번의 fetch_reviews() 호출로 처리한다 — 브라우저 한 번만
띄우고 재사용(테넌트 로스터 일괄 보강용).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("external.kakao_place")


@dataclass
class PlaceReview:
    """장소 한 곳의 리뷰 집계."""
    place_id: str
    평점: float | None = None          # 카카오맵 평균 별점 (0~5)
    후기수: int = 0                     # 카카오맵 후기 수
    블로그리뷰수: int = 0               # 블로그 리뷰 수
    사진수: int = 0
    ok: bool = False                    # 데이터 확보 성공 여부

    def as_dict(self) -> dict:
        return {"place_id": self.place_id, "평점": self.평점,
                "후기수": self.후기수, "블로그리뷰수": self.블로그리뷰수,
                "사진수": self.사진수, "ok": self.ok}


class KakaoPlaceScraper:
    """place.map.kakao.com 리뷰 스크래퍼. place_id 리스트 → PlaceReview 리스트."""

    PANEL3 = "place-api.map.kakao.com/places/panel3/"
    PLACE_URL = "https://place.map.kakao.com/{}"

    def __init__(self, *, headless: bool = True, timeout_ms: int = 20000,
                 wait_after_ms: int = 800):
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._wait_after_ms = wait_after_ms

    def fetch_reviews(self, place_ids: list[str]) -> list[PlaceReview]:
        """place_id 리스트의 리뷰 집계를 수집한다 (브라우저 1회 재사용).

        Playwright 미설치/실행 실패 시 RuntimeError를 던진다(상위에서 처리).
        개별 place의 패널 응답이 없으면 ok=False로 담아 계속 진행한다.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "KakaoPlaceScraper는 playwright가 필요합니다 "
                "(pip install playwright && playwright install chromium)") from e

        results: list[PlaceReview] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self._headless)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36"))
            page = ctx.new_page()
            for pid in place_ids:
                results.append(self._fetch_one(page, str(pid)))
            browser.close()
        ok = sum(1 for r in results if r.ok)
        logger.info("kakao_place: %d개 중 %d개 리뷰 확보", len(place_ids), ok)
        return results

    def _fetch_one(self, page, pid: str) -> PlaceReview:
        review = PlaceReview(place_id=pid)
        try:
            with page.expect_response(
                lambda r, _pid=pid: f"{self.PANEL3}{_pid}" in r.url,
                timeout=self._timeout_ms,
            ) as info:
                page.goto(self.PLACE_URL.format(pid),
                          wait_until="domcontentloaded", timeout=self._timeout_ms)
            data = info.value.json()
        except Exception as e:
            logger.debug("place %s 패널 응답 실패: %s", pid, e)
            return review

        km = (data.get("kakaomap_review") or {}).get("score_set") or {}
        blog = data.get("blog_review") or {}
        if km or blog:
            review.평점 = km.get("average_score")
            review.후기수 = km.get("review_count") or 0
            review.사진수 = km.get("photo_count") or 0
            review.블로그리뷰수 = blog.get("review_count") or 0
            review.ok = True
        return review
