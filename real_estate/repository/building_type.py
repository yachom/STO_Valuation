"""건물유형 분류 — 상가형 / 오피스형 / 혼합형.

중심 테넌트(앵커) 식별 전략을 라우팅하기 위한 0단계. 유형에 따라 신호가
완전히 다르다:
    - 상가형: 카카오 리뷰·평점·집객(리뷰 기반 스코어링)
    - 오피스형: 카카오에 입주 기업이 안 잡힘 → 뉴스/웹 서사 기반

신호:
    1. 건축물대장 주용도(main_use) — 강한 prior.
       업무시설=오피스, (제1·2종)근린생활시설·판매시설=상가.
    2. 카카오 입점업체 카테고리 분포 — 보조.
       소비자대면(음식점·의료·생활·카페) 많으면 상가, 금융/공유오피스 위주에
       소비자 테넌트 희박하면 오피스.
"""

from __future__ import annotations

import collections

# 카카오 카테고리 대분류 → 소비자대면(상가) 신호
_CONSUMER_CATS = {"음식점", "의료,건강", "가정,생활", "스포츠,레저",
                  "문화,여가", "여행,교통"}
# 오피스 신호 (대면 영업점도 일부 섞이나, 순수 소비보다 약함)
_OFFICE_CATS = {"금융,보험", "서비스,산업", "부동산"}


def classify_building_type(main_use: str | None, tenants: list[dict]) -> dict:
    """건물유형을 분류하고 앵커 전략을 추천한다.

    Args:
        main_use: 건축물대장 주용도 (Building.main_use). 없으면 roster만 사용.
        tenants:  카카오 입점업체 로스터 (각 dict에 "카테고리" 키).

    Returns:
        {
            "유형": "상가형" | "오피스형" | "혼합형",
            "앵커전략": "리뷰기반" | "뉴스기반" | "혼합",
            "신뢰도": "높음" | "보통" | "낮음",
            "근거": {main_use, 소비자비율, 소비자수, 오피스수, 업체수},
        }
    """
    mu = main_use or ""
    office_mu = "업무" in mu
    retail_mu = ("근린생활" in mu) or ("판매" in mu)

    dist = collections.Counter(
        (t.get("카테고리") or "").split(" > ")[0] for t in tenants)
    total = sum(dist.values())
    consumer = sum(v for k, v in dist.items() if k in _CONSUMER_CATS)
    office_ish = sum(v for k, v in dist.items() if k in _OFFICE_CATS)
    consumer_ratio = round(consumer / total, 3) if total else 0.0

    # roster 기반 판정 (소비자 테넌트가 풍부하면 상가)
    roster_retail = consumer >= 5 and consumer_ratio >= 0.5
    roster_office = total > 0 and consumer < 3 and office_ish >= consumer

    # main_use(강한 prior)와 roster를 결합.
    if office_mu and not retail_mu:
        type_, conf = ("오피스형", "높음" if roster_office or total < 5 else "보통")
        if roster_retail:                       # 업무시설인데 소비자 테넌트 풍부 → 혼합
            type_, conf = "혼합형", "보통"
    elif retail_mu and not office_mu:
        type_, conf = ("상가형", "높음" if roster_retail else "보통")
    elif office_mu and retail_mu:               # 주용도가 복합
        type_, conf = "혼합형", "보통"
    else:                                       # main_use 없음/모호 → roster로만
        if roster_retail:
            type_, conf = "상가형", "보통"
        elif roster_office:
            type_, conf = "오피스형", "보통"
        else:
            type_, conf = "혼합형", "낮음"

    strategy = {"상가형": "리뷰기반", "오피스형": "뉴스기반",
                "혼합형": "혼합"}[type_]

    return {
        "유형": type_,
        "앵커전략": strategy,
        "신뢰도": conf,
        "근거": {
            "main_use": main_use,
            "소비자비율": consumer_ratio,
            "소비자수": consumer,
            "오피스수": office_ish,
            "업체수": total,
            "카테고리분포": dict(dist),
        },
    }
