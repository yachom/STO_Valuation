"""인지도 Agent.

데이터:
- 네이버 통합검색(블로그/뉴스/이미지/local/cafe) total
- 네이버 SearchAd 키워드툴 월간 검색량/연관 키워드
- 카카오 로컬 키워드 검색 결과 수
"""

from __future__ import annotations

from typing import Any

from ..tools import kakao_local, naver_search, naver_searchad
from . import _llm

SYSTEM = """당신은 부동산 STO 사회적 가치 평가 중 '인지도(Awareness)' 축을 담당하는 평가 에이전트다.
다음 신호로 평가하라:
- 네이버 통합검색 채널별 검색 결과량(블로그/뉴스/이미지/local/cafe)
- 네이버 SearchAd 키워드 도구의 PC·모바일 월간 검색량
- 카카오 로컬 키워드 검색에서의 매칭 수
인지도는 '브랜드/장소명' 단위로 평가하라. 단순 주소 검색은 부수적이다.
강한 검색 트래픽·다양한 채널·뚜렷한 연관 키워드 = 높은 인지도.
점수는 보수적으로 0~100으로 매겨라.
"""


def _candidate_queries(metadata: dict[str, Any]) -> list[str]:
    addr = metadata.get("address", {}) or {}
    name = addr.get("building_name")
    road = addr.get("road")
    queries: list[str] = []
    if name:
        queries.append(name)
        sigungu = addr.get("sigungu")
        if sigungu:
            queries.append(f"{sigungu} {name}")
    if road:
        queries.append(road)
    if addr.get("jibun"):
        queries.append(addr["jibun"])
    return list(dict.fromkeys(q for q in queries if q))[:4]


def run(metadata: dict[str, Any], callbacks: list[Any] | None = None) -> dict[str, Any]:
    queries = _candidate_queries(metadata)
    primary = queries[0] if queries else (metadata.get("input_address") or "")

    naver_searches: dict[str, Any] = {}
    for q in queries:
        naver_searches[q] = naver_search.search_all_channels(q)

    keyword_tool = naver_searchad.keyword_tool(primary) if primary else {"skipped": "no query"}

    coord = metadata.get("coord") or {}
    kakao_keyword = (
        kakao_local.keyword_search(primary, x=coord.get("x"), y=coord.get("y"), radius=1000)
        if primary
        else {"skipped": "no query"}
    )

    evidence = {
        "queries": queries,
        "naver_search_by_query": naver_searches,
        "naver_searchad_keyword_tool": keyword_tool,
        "kakao_local_keyword": kakao_keyword,
    }

    scored = _llm.score_with_llm(
        SYSTEM, evidence, axis_label="인지도", callbacks=callbacks
    )
    return {"axis": "AWARENESS", "evidence": evidence, **scored}
