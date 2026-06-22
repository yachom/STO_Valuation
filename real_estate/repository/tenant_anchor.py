"""상가형 건물의 중심 테넌트(앵커) 스코어링.

건물유형이 '상가형'(또는 혼합형의 리테일 파트)일 때, 입점업체 로스터를
앵커 점수로 순위화한다. 단일 신호(후기수)는 의료 앵커를 놓치므로(치과는
카카오후기 0인데 블로그 422) 멀티신호를 투명하게 가중합한다:

    집객  = log(후기수) + log(블로그리뷰수)      — 트래픽/인지 대용 (카카오)
    평점  = 카카오 평균 별점 / 5                 — 품질 (카카오)
    브랜드 = 전국 프랜차이즈/브랜드 여부          — 집객 앵커 (카카오)
    업종매칭 = 상권 주력 업종과 일치 여부          — 상권 매출구조 교차참조
    면적  = 점포 면적 / 건물 내 최대             — 대형 점포 앵커 (LocalData 인허가)
    존속  = 영업중 가게의 영업연수               — 생존 검증 앵커 (LocalData 인허가)

카카오 단독 4신호로 출발하고, LocalData 인허가(turnover)가 결합되면 면적·존속
2신호가 추가된다 — 인허가는 같은 상호를 매칭해 '이 가게가 얼마나 크고 오래
버텼나'를 보탠다. 인허가 데이터가 없으면(키 없음) 카카오 4신호만으로 동작한다
(하위호환). 매칭은 상호명으로 하므로 업종 API(SERVICE_CATALOG)가 늘수록
면적·존속 커버리지가 자동으로 넓어진다.

오피스형 앵커는 이 모듈을 쓰지 않는다(리뷰가 무의미) — 뉴스 기반 별도 경로.
"""

from __future__ import annotations

import math
import re
from datetime import date

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
# 인허가(turnover) 결합 시 — 카카오 신호 합 0.75 유지 + 면적·존속 0.25 추가.
_TURNOVER_WEIGHTS = {"집객": 0.34, "평점": 0.11, "브랜드": 0.15, "업종매칭": 0.15,
                     "면적": 0.15, "존속": 0.10}
_TENURE_CAP_YEARS = 10.0     # 존속연수 정규화 상한 (10년 이상이면 1.0)


_WS_RE = re.compile(r"\s+")
_CORP_RE = re.compile(r"주식회사|유한회사|\(주\)|\(유\)|㈜|㈜")
_PAREN_RE = re.compile(r"\([^)]*\)")


def _norm_name(s: str) -> str:
    """상호 정규화 — 법인 접두((주)·주식회사)·괄호·공백 제거로 카카오↔인허가 매칭률↑.

    카카오 '교촌치킨 마곡역점' vs 인허가 BPLC_NM '(주)교촌에프앤비 …' 같은 표기
    차이를 흡수. 의료/법인 임차인(의료법인·㈜) 매칭 대비.
    """
    s = (s or "").strip().lower()
    s = _CORP_RE.sub("", s)
    s = _PAREN_RE.sub("", s)
    return _WS_RE.sub("", s)


def _index_licenses(licenses: list) -> dict[str, object]:
    """영업중 인허가를 상호 정규화명 → 대표 레코드로 인덱싱.

    같은 상호가 여럿이면 면적이 가장 큰 레코드를 대표로(앵커는 대형 점포).
    """
    idx: dict[str, object] = {}
    for lic in licenses or []:
        if getattr(lic, "is_closed", False):
            continue
        key = _norm_name(getattr(lic, "상호", ""))
        if len(key) < 2:
            continue
        cur = idx.get(key)
        if cur is None or (getattr(lic, "면적", None) or 0) > (getattr(cur, "면적", None) or 0):
            idx[key] = lic
    return idx


def _match_license(name: str, idx: dict[str, object]):
    """테넌트 상호 → 인허가 레코드 (정확 일치 → 포함 매칭, 최소 3자)."""
    key = _norm_name(name)
    if not key:
        return None
    if key in idx:
        return idx[key]
    for lname, lic in idx.items():        # 포함 매칭 ('스타벅스' ⊂ '스타벅스강서점')
        if min(len(key), len(lname)) >= 3 and (lname in key or key in lname):
            return lic
    return None


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


def _extract_signals(tenants: list[dict], *, top_industries: list[str],
                     lic_idx: dict[str, object], use_turnover: bool,
                     today: date) -> list[dict]:
    """입점업체별 원천 신호를 추출한다 (순수 — DB/IO 없음).

    카카오 리뷰(집객·평점)·브랜드·업종매칭에 더해, 인허가 결합 시 상호
    매칭으로 면적·영업연수를 보탠다. 정규화 전 raw 값을 담은 row 리스트 반환.
    """
    rows = []
    for t in tenants:
        rv = t.get("리뷰") or {}
        후기 = rv.get("후기수") or 0
        블로그 = rv.get("블로그리뷰수") or 0
        평점 = rv.get("평점")
        traffic_raw = math.log1p(후기) + math.log1p(블로그)
        lic = _match_license(t.get("상호", ""), lic_idx) if use_turnover else None
        면적 = getattr(lic, "면적", None) if lic else None
        인허가일 = getattr(lic, "인허가일자", None) if lic else None
        영업연수 = (round((today - 인허가일).days / 365.25, 1)
                  if 인허가일 else None)
        rows.append({
            "t": t, "후기": 후기, "블로그": 블로그, "평점": 평점,
            "traffic_raw": traffic_raw,
            "brand": _is_brand(t.get("상호", ""), t.get("카테고리", "")),
            "industry": _industry_match(t.get("상호", ""), t.get("카테고리", ""),
                                        top_industries),
            "면적": 면적, "영업연수": 영업연수, "matched": lic is not None,
        })
    return rows


def _normalize(rows: list[dict]) -> tuple[float, float]:
    """정규화 기준(건물 내 최대 트래픽·면적)을 구한다 (순수 — DB/IO 없음).

    0 분모 방지를 위해 최댓값이 0이면 1.0으로 떨어뜨린다.
    """
    max_traffic = max((r["traffic_raw"] for r in rows), default=0.0) or 1.0
    max_area = max((r["면적"] for r in rows if r["면적"]), default=0.0) or 1.0
    return max_traffic, max_area


def _compute_scores(rows: list[dict], *, max_traffic: float, max_area: float,
                    w: dict, use_turnover: bool) -> list[dict]:
    """row별 가중합 앵커점수를 계산해 결과 dict 리스트를 만든다 (순수 — DB/IO 없음).

    면적·존속은 매칭된 가게에만(미매칭=0, 양의 신호라 페널티 아님).
    정렬·등급 부여 전 상태의 항목 리스트를 반환한다.
    """
    scored = []
    for r in rows:
        traffic_n = r["traffic_raw"] / max_traffic
        rating_n = (r["평점"] / 5.0) if r["평점"] else 0.5  # 무평점=중립
        score = (w["집객"] * traffic_n + w["평점"] * rating_n
                 + w["브랜드"] * (1.0 if r["brand"] else 0.0)
                 + w["업종매칭"] * (1.0 if r["industry"] else 0.0))
        if use_turnover:
            area_n = (r["면적"] / max_area) if r["면적"] else 0.0
            tenure_n = (min(r["영업연수"] / _TENURE_CAP_YEARS, 1.0)
                        if r["영업연수"] else 0.0)
            score += w["면적"] * area_n + w["존속"] * tenure_n
        item = dict(r["t"])
        item["앵커점수"] = round(score, 4)
        item["신호"] = {"후기수": r["후기"], "블로그리뷰수": r["블로그"],
                       "평점": r["평점"], "브랜드": r["brand"],
                       "업종매칭": r["industry"], "면적": r["면적"],
                       "영업연수": r["영업연수"], "인허가매칭": r["matched"]}
        scored.append(item)
    return scored


def _rank_and_grade(scored: list[dict]) -> list[dict]:
    """앵커점수 내림차순 정렬 후 등급을 매긴다 (순수 — DB/IO 없음).

    상위 20%는 "앵커"(점수 ≥0.3 컷), 그 다음 30%는 "주요", 나머지는 "일반".
    입력 리스트를 제자리 정렬·수정하고 같은 리스트를 반환한다.
    """
    scored.sort(key=lambda x: x["앵커점수"], reverse=True)

    n = len(scored)
    for i, item in enumerate(scored):
        if i < max(1, round(n * 0.2)) and item["앵커점수"] >= 0.3:
            item["앵커등급"] = "앵커"
        elif i < max(1, round(n * 0.5)):
            item["앵커등급"] = "주요"
        else:
            item["앵커등급"] = "일반"
    return scored


def score_tenants(tenants: list[dict], *, top_industries: list[str] | None = None,
                  licenses: list | None = None, weights: dict | None = None,
                  today: date | None = None) -> list[dict]:
    """입점업체를 앵커 점수로 순위화한다.

    Args:
        tenants: 각 dict에 "상호","카테고리","리뷰"(평점·후기수·블로그리뷰수) 포함.
        top_industries: 상권 주력 업종명 리스트 (CommercialSalesRepository 결과).
        licenses: LocalData 영업 인허가(BusinessLicense) 리스트 — 상호 매칭으로
            면적·존속 신호를 보탠다. None이면 카카오 4신호만(하위호환).
        weights: 신호 가중치 override.
        today: 영업연수 기준일 (테스트용).

    Returns:
        anchor_score 내림차순 정렬된 dict 리스트. 각 항목에 추가:
            "앵커점수": float(0~1), "앵커등급": "앵커"|"주요"|"일반",
            "신호": {집객, 평점, 브랜드, 업종매칭, 면적, 영업연수, 인허가매칭}
    """
    top_industries = top_industries or []
    today = today or date.today()
    lic_idx = _index_licenses(licenses)
    # 인허가 매칭이 하나라도 되면 면적·존속 신호를 켠다 (없으면 하위호환 가중치).
    use_turnover = bool(lic_idx)
    w = {**(_TURNOVER_WEIGHTS if use_turnover else _DEFAULT_WEIGHTS), **(weights or {})}

    # 1) 원천 신호 추출 → 2) 정규화 기준 → 3) 가중합 → 4) 정렬·등급
    rows = _extract_signals(tenants, top_industries=top_industries,
                            lic_idx=lic_idx, use_turnover=use_turnover,
                            today=today)
    max_traffic, max_area = _normalize(rows)
    scored = _compute_scores(rows, max_traffic=max_traffic, max_area=max_area,
                             w=w, use_turnover=use_turnover)
    return _rank_and_grade(scored)
