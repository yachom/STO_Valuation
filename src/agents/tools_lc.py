"""LangChain @tool 래퍼. Retriever ReAct 루프가 호출.

각 도구는 metadata와 KnowledgeGraph를 클로저로 캡쳐한다.
LLM에 전달되는 결과는 토큰을 아끼기 위해 핵심만 직렬화.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from ..kg.graph_store import KnowledgeGraph
from ..tools import gseed as gseed_tool
from ..tools import kakao_local
from ..tools import molit_trade
from ..tools import naver_search
from ..tools import naver_searchad
from ..tools import sbiz


def _trim(obj: Any, limit: int = 6000) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def _safe(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)[:300]}


def build_tools(metadata: dict[str, Any], kg: KnowledgeGraph | None):
    coord = metadata.get("coord") or {}
    cx, cy = coord.get("x"), coord.get("y")
    sigungu_cd = (metadata.get("identifiers") or {}).get("sigungu_cd")
    addr = metadata.get("address", {}) or {}

    @tool("get_subject_metadata", parse_docstring=True)
    def get_subject_metadata() -> str:
        """대상 자산의 정규화 메타데이터(주소, 식별자, 건물·토지 요약, 좌표) 반환."""
        compact = {
            "address": metadata.get("address"),
            "identifiers": metadata.get("identifiers"),
            "coord": coord,
            "building_title_first": _first_building_item(metadata),
            "land_brief": _land_brief(metadata),
        }
        return _trim(compact)

    @tool("gseed_lookup", parse_docstring=True)
    def gseed_lookup(address_keyword: str) -> str:
        """G-SEED 녹색건축 인증을 주소 키워드로 조회한다.

        Args:
            address_keyword: 인증현황 DB 매칭용 주소 일부. 예: '강남구 역삼동'.
        """
        return _trim(_safe(gseed_tool.search_by_address, address_keyword))

    @tool("naver_web_search", parse_docstring=True)
    def naver_web_search(query: str) -> str:
        """네이버 통합검색 6채널(블로그/뉴스/이미지/local/cafe/webkr) total과 미리보기 반환.

        Args:
            query: 검색할 키워드(브랜드명/장소명/지명 등).
        """
        return _trim(_safe(naver_search.search_all_channels, query))

    @tool("naver_keyword_volume", parse_docstring=True)
    def naver_keyword_volume(hint_keyword: str) -> str:
        """네이버 SearchAd 키워드툴: PC·모바일 월간 검색량과 연관 키워드.

        Args:
            hint_keyword: 시드 키워드.
        """
        return _trim(_safe(naver_searchad.keyword_tool, hint_keyword))

    @tool("kakao_keyword_places", parse_docstring=True)
    def kakao_keyword_places(query: str, radius_m: int = 1000) -> str:
        """카카오 로컬 키워드 검색. 자산 좌표 기준 반경 안에서.

        Args:
            query: 검색어(브랜드명 등).
            radius_m: 검색 반경 미터. 기본 1000.
        """
        return _trim(_safe(kakao_local.keyword_search, query, x=cx, y=cy, radius=radius_m))

    @tool("kakao_poi_radius", parse_docstring=True)
    def kakao_poi_radius(radius_m: int = 300) -> str:
        """자산 좌표 기준 반경 내 카카오 카테고리 POI 카운트.

        Args:
            radius_m: 반경 미터. 100/300/500/1000 권장.
        """
        if not (cx and cy):
            return json.dumps({"skipped": "no coord"}, ensure_ascii=False)
        return _trim(_safe(kakao_local.poi_radius_summary, cx, cy, radius=radius_m))

    @tool("sbiz_stores_radius", parse_docstring=True)
    def sbiz_stores_radius(radius_m: int = 300) -> str:
        """소상공인 상권정보: 반경 내 업소 분포(대/소분류 카운트).

        Args:
            radius_m: 반경 미터.
        """
        if not (cx and cy):
            return json.dumps({"skipped": "no coord"}, ensure_ascii=False)
        return _trim(_safe(sbiz.stores_in_radius, cx, cy, radius_m=radius_m))

    @tool("molit_transactions", parse_docstring=True)
    def molit_transactions(months: int = 12, property_type: str = "상업업무용") -> str:
        """국토부 실거래가(시군구 단위, 최근 N개월).

        Args:
            months: 최근 개월 수.
            property_type: 부동산 유형(상업업무용/오피스텔/아파트 등).
        """
        if not sigungu_cd:
            return json.dumps({"skipped": "no sigungu_cd"}, ensure_ascii=False)
        return _trim(_safe(molit_trade.fetch_transactions, sigungu_cd, property_type=property_type, months=months))

    @tool("graph_db_axis_evidence", parse_docstring=True)
    def graph_db_axis_evidence(axis: str, top_k: int = 10) -> str:
        """뉴스 기반 지식그래프에서 특정 축(ESG|AWARENESS|VITALITY)에 영향 주는 이벤트/이슈 조회.

        Args:
            axis: ESG, AWARENESS, VITALITY 중 하나.
            top_k: 반환 개수.
        """
        if kg is None:
            return json.dumps({"skipped": "no knowledge graph"}, ensure_ascii=False)
        return _trim(kg.axis_evidence(axis, top_k=top_k))

    @tool("graph_db_search", parse_docstring=True)
    def graph_db_search(query: str) -> str:
        """지식그래프 엔티티 이름 부분일치 검색.

        Args:
            query: 검색할 엔티티 이름 키워드.
        """
        if kg is None:
            return json.dumps({"skipped": "no knowledge graph"}, ensure_ascii=False)
        return _trim(kg.search_entities(query))

    @tool("graph_db_neighbors", parse_docstring=True)
    def graph_db_neighbors(entity_name: str) -> str:
        """지식그래프에서 특정 엔티티의 이웃 관계 조회.

        Args:
            entity_name: 엔티티 정확한 이름(또는 거의 정확).
        """
        if kg is None:
            return json.dumps({"skipped": "no knowledge graph"}, ensure_ascii=False)
        return _trim(kg.neighbors(entity_name))

    return [
        get_subject_metadata,
        gseed_lookup,
        naver_web_search,
        naver_keyword_volume,
        kakao_keyword_places,
        kakao_poi_radius,
        sbiz_stores_radius,
        molit_transactions,
        graph_db_axis_evidence,
        graph_db_search,
        graph_db_neighbors,
    ]


def _first_building_item(metadata: dict[str, Any]) -> dict[str, Any]:
    title = ((metadata.get("building") or {}).get("title") or {})
    resp = title.get("response") if isinstance(title, dict) else None
    items = ((resp or {}).get("body") or {}).get("items") if isinstance(resp, dict) else None
    if isinstance(items, dict):
        items = items.get("item")
    if isinstance(items, list) and items:
        first = items[0]
        return {
            k: first.get(k)
            for k in (
                "mainPurpsCdNm",
                "strctCdNm",
                "grndFlrCnt",
                "ugrndFlrCnt",
                "totArea",
                "useAprDay",
                "vltnBldYn",
            )
        }
    return {}


def _land_brief(metadata: dict[str, Any]) -> dict[str, Any]:
    land = metadata.get("land") or {}
    out: dict[str, Any] = {}
    for key in ("land_characteristics", "land_use", "official_land_price"):
        payload = land.get(key) or {}
        if isinstance(payload, dict):
            resp = payload.get("response") or payload
            body = (resp or {}).get("body") if isinstance(resp, dict) else None
            items = (body or {}).get("items") if isinstance(body, dict) else None
            if isinstance(items, dict):
                items = items.get("item")
            if isinstance(items, list) and items:
                out[key] = items[0]
    return out
