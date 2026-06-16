"""상가형 건물의 중심 테넌트(앵커) 스코어링.

건물유형이 '상가형'(또는 혼합형의 리테일 파트)일 때, 입점업체 로스터를
앵커 점수로 순위화한다. 단일 신호(후기수)는 의료 앵커를 놓치므로(치과는
카카오후기 0인데 블로그 422) 멀티신호를 투명하게 가중합한다:

    집객  = log(후기수) + log(블로그리뷰수)      — 트래픽/인지 대용
    평점  = 카카오 평균 별점 / 5                 — 품질
    브랜드 = 전국 프랜차이즈/브랜드 여부          — 집객 앵커
    업종매칭 = 상권 주력 업종과 일치 여부          — 상권 매출구조 교차참조

오피스형 앵커는 이 모듈을 쓰지 않는다(리뷰가 무의미) — 뉴스 기반 별도 경로.
"""

from __future__ import annotations

import math

# 집객 앵커가 되는 대표 전국 브랜드 (place_name/카테고리 leaf 매칭). 완전 목록이
# 아니라 고신뢰 시드 — 미매칭은 카테고리 깊이 휴리스틱으로 보완.
_KNOWN_BRANDS = {
    "올리브영", "다이소", "스타벅스", "투썸플레이스", "메가커피", "공차", "이디야",
    "파리바게뜨", "뚜레쥬르", "이삭토스트", "세븐일레븐", "GS25", "CU", "이마트",
    "이마트24", "롯데마트", "홈플러스", "CGV", "롯데시네마", "메가박스",
    "KB국민은행", "신한은행", "하나은행", "우리은행", "NH농협은행", "IBK기업은행",
    "맥도날드", "버거킹", "롯데리아", "교촌치킨", "BBQ", "스타벅스", "올리브영",
}

_DEFAULT_WEIGHTS = {"집객": 0.45, "평점": 0.15, "브랜드": 0.20, "업종매칭": 0.20}


def _is_brand(name: str, category: str) -> bool:
    """전국 브랜드/프랜차이즈 추정. 알려진 브랜드명 매칭 + 카테고리 깊이 휴리스틱.

    카카오는 프랜차이즈의 브랜드명을 카테고리 최하위 레벨로 붙인다
    (예: '편의점 > 세븐일레븐', '음식점 > 간식 > 토스트 > 이삭토스트').
    """
    hay = f"{name} {category}"
    # 알려진 전국 브랜드만 인정. (카테고리 깊이 휴리스틱은 비프랜차이즈
    # 오탐이 심해 제거 — 여자만장어덮밥 같은 깊은 일반 카테고리를 브랜드로
    # 잘못 플래그했다. 미등록 프랜차이즈는 집객 신호로 자연히 순위에 반영.)
    return any(b in hay for b in _KNOWN_BRANDS)


def _industry_match(name: str, category: str, top_industries: list[str]) -> bool:
    """테넌트가 상권 주력 업종과 일치하는지 (키워드 중첩)."""
    if not top_industries:
        return False
    hay = f"{name} {category}"
    for ind in top_industries:
        # 업종명 핵심 토큰(예: '한식음식점'→'한식', '치과의원'→'치과')과 중첩 확인.
        core = ind.replace("음식점", "").replace("의원", "").replace("점", "").strip()
        if core and (core in hay or ind in hay):
            return True
    return False


def score_tenants(tenants: list[dict], *, top_industries: list[str] | None = None,
                  weights: dict | None = None) -> list[dict]:
    """입점업체를 앵커 점수로 순위화한다.

    Args:
        tenants: 각 dict에 "상호","카테고리","리뷰"(평점·후기수·블로그리뷰수) 포함.
        top_industries: 상권 주력 업종명 리스트 (CommercialSalesRepository 결과).
        weights: 신호 가중치 override.

    Returns:
        anchor_score 내림차순 정렬된 dict 리스트. 각 항목에 추가:
            "앵커점수": float(0~1), "앵커등급": "앵커"|"주요"|"일반",
            "신호": {집객, 평점, 브랜드, 업종매칭} (정규화 전 원천값/플래그)
    """
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}
    top_industries = top_industries or []

    # 1) 원천 신호 추출
    rows = []
    for t in tenants:
        rv = t.get("리뷰") or {}
        후기 = rv.get("후기수") or 0
        블로그 = rv.get("블로그리뷰수") or 0
        평점 = rv.get("평점")
        traffic_raw = math.log1p(후기) + math.log1p(블로그)
        rows.append({
            "t": t, "후기": 후기, "블로그": 블로그, "평점": 평점,
            "traffic_raw": traffic_raw,
            "brand": _is_brand(t.get("상호", ""), t.get("카테고리", "")),
            "industry": _industry_match(t.get("상호", ""), t.get("카테고리", ""),
                                        top_industries),
        })

    # 2) 집객 정규화 (건물 내 최대 대비)
    max_traffic = max((r["traffic_raw"] for r in rows), default=0.0) or 1.0

    # 3) 가중합
    scored = []
    for r in rows:
        traffic_n = r["traffic_raw"] / max_traffic
        rating_n = (r["평점"] / 5.0) if r["평점"] else 0.5  # 무평점=중립
        score = (w["집객"] * traffic_n + w["평점"] * rating_n
                 + w["브랜드"] * (1.0 if r["brand"] else 0.0)
                 + w["업종매칭"] * (1.0 if r["industry"] else 0.0))
        item = dict(r["t"])
        item["앵커점수"] = round(score, 4)
        item["신호"] = {"후기수": r["후기"], "블로그리뷰수": r["블로그"],
                       "평점": r["평점"], "브랜드": r["brand"],
                       "업종매칭": r["industry"]}
        scored.append(item)

    scored.sort(key=lambda x: x["앵커점수"], reverse=True)

    # 4) 등급 — 상위 20%/그 다음 30% (최소 컷 적용)
    n = len(scored)
    for i, item in enumerate(scored):
        if i < max(1, round(n * 0.2)) and item["앵커점수"] >= 0.3:
            item["앵커등급"] = "앵커"
        elif i < max(1, round(n * 0.5)):
            item["앵커등급"] = "주요"
        else:
            item["앵커등급"] = "일반"
    return scored
