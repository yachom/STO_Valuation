"""Harvester — 검색 실행 → WebDoc 적재 → Fact/Entity 추출 → 그래프 연결 (설계 §9).

오염 방지(D9)의 구현 지점: **LLM은 연결을 결정하지 않는다.** LLM은 mentions에
표면형(surface)+추정만 제시하고, 연결 판정은 이 모듈의 결정적 코드가 한다:
    - 정규화 이름이 스코프 노드와 정확히 일치          → MENTIONS (확정)
    - 포함 관계 등 이름 유사뿐                         → Entity 신설 + SAME_AS_CANDIDATE
    - 불일치 / code_clues(시군구) 모순                 → Entity 단독 (MENTIONS→Entity)

confidence = source_tier × extraction_confidence (§9.3). 채널(vertical)마다
출처 신뢰 prior(source_tier)가 다르다 — news=高, webkr=中高, blog=中,
cafe/kin=低. 저신뢰 소스는 자동으로 낮은 confidence를 받아 기존 confidence
게이트·corroboration이 down-weight한다. 티어 매핑은 _SOURCE_TIER 한 곳에 둔다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from neo4j import Driver
from pydantic import BaseModel, Field, field_validator

from real_estate.graphflow.agents.base import (BaseAgent, coerce_json,
                                               normalize_text, short_hash)
from real_estate.graphflow.prompts import get_prompt
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import KEY_PROP
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.seeder import ScopeGraph, SeedEdge, write_edges, seed
from infra.tool.naver import (
    NaverNewsScrapeTool,
    NaverNewsSearchTool,
    NaverVerticalSearchTool,
)
from infra.agent.tracing import emit

logger = logging.getLogger("graphflow.harvester")

SOURCE_TIER_NEWS = 0.8          # 주요 언론 (§9.3). news 스크랩 경로 앵커 — 불변
SOURCE_TIER_PROVISIONAL = 0.5   # 발행자 제공 자료 — 교차 검증 전 (§10.5, D14)

# ── 출처 신뢰 티어 (confidence = source_tier × extraction_confidence) ──
# 채널별 신뢰 prior. news(0.8)를 앵커로, 결정 확정된 상대 배수
# (news×1.0 / web×0.85 / blog×0.7 / cafe·kin×0.55)를 곱해 절대 티어로 환산한다.
# 한 곳(이 dict)에서만 조정 → 저신뢰 소스가 자동으로 낮은 confidence를 받고
# 기존 게이트·corroboration이 down-weight한다.
SOURCE_TIER_WEB = round(SOURCE_TIER_NEWS * 0.85, 3)    # 일반 웹(Tavily)·webkr = 0.68
SOURCE_TIER_BLOG = round(SOURCE_TIER_NEWS * 0.70, 3)   # 블로그 = 0.56
SOURCE_TIER_FORUM = round(SOURCE_TIER_NEWS * 0.55, 3)  # 카페·지식iN = 0.44

# source kind(=수집 채널) → 티어. _harvest_snippets 가 vertical 별로 참조.
_SOURCE_TIER = {
    "news": SOURCE_TIER_NEWS,           # 0.8  (스크랩 경로, 별도 처리)
    "web": SOURCE_TIER_WEB,             # 0.68 (Tavily general)
    "webkr": SOURCE_TIER_WEB,           # 0.68 (네이버 웹문서)
    "blog": SOURCE_TIER_BLOG,           # 0.56
    "cafearticle": SOURCE_TIER_FORUM,   # 0.44
    "kin": SOURCE_TIER_FORUM,           # 0.44 (지식iN)
}

# 스니펫(스크랩 불필요) vertical 들 — news 는 스크랩 경로라 제외.
_SNIPPET_VERTICALS = ("webkr", "blog", "cafearticle")

_MAX_ARTICLES_PER_QUERY = 5     # 쿼리당 본문 스크랩·추출 상한 (gpt-mini 저비용 → 공격적 수집)
_MAX_FACTS_PER_DOC = 8
_BODY_TRIM = 3500               # 추출 프롬프트에 넣는 본문 길이 상한

# 공유 헬퍼(agents.base)를 모듈 로컬 이름으로 — 호출부(surface 매칭·문서 해시) 다수.
_norm = normalize_text
_short_hash = short_hash


# ───────────────────────────────────────────────────────────
# Fact 추출 스키마 (토의 확정안)
# ───────────────────────────────────────────────────────────
class MentionDraft(BaseModel):
    surface: str = Field(description="기사에 등장한 표면형 그대로 (예: '강남역 상권')")
    guess_label: str | None = Field(
        default=None,
        description="추정 종류: CommercialArea/AdminDong/Industry/Building/Complex/"
                    "DevelopmentProject/브랜드/정책/기타")
    sigungu: str | None = Field(default=None, description="기사에서 확인되는 시군구 (있으면)")


class FactDraft(BaseModel):
    text: str = Field(description="원자적 사실 1문장. 기사에 명시된 내용만, 추측 금지")
    mentions: list[MentionDraft] = Field(description="이 사실이 언급하는 엔티티들")
    event_date: str | None = Field(default=None, description="사실이 가리키는 시점 YYYY-MM(-DD)")
    valid_until: str | None = Field(
        default=None,
        description="원문에 명시된 유효기간 YYYY-MM-DD ('2027년 완공 예정'→2027-12-31). 명시 없으면 null")
    temporal_kind: Literal["static", "dated", "forecast"] = Field(
        description="static=불변 사실 / dated=특정 시점 사실 / forecast=예정·전망")
    extraction_confidence: float = Field(description="추출 확신도 0~1")


class ExtractionOutput(BaseModel):
    facts: list[FactDraft] = Field(description=f"관련 사실 최대 {_MAX_FACTS_PER_DOC}개. 무관하면 빈 배열")
    doc_relevance: float = Field(description="기사 전체가 검색 맥락과 관련된 정도 0~1")

    @field_validator("facts", mode="before")
    @classmethod
    def _coerce(cls, v):
        return coerce_json(v)


# ───────────────────────────────────────────────────────────
# 사전 필터 — 스크랩/추출 전에 제목+요약만으로 관련 기사 선별 (Haiku, 저비용)
# ───────────────────────────────────────────────────────────
class PrefilterOutput(BaseModel):
    relevant_indexes: list[int] = Field(
        description="검색 맥락과 관련된 기사의 번호들 (0-base). 없으면 빈 배열")

    @field_validator("relevant_indexes", mode="before")
    @classmethod
    def _coerce(cls, v):
        return coerce_json(v)


class _Prefilter(BaseAgent):
    name = "prefilter"

    def select(self, *, query: str, target_card: str, items: list[dict]) -> list[int]:
        lines = [f"[{i}] {it.get('title')} — {it.get('description')}"
                 for i, it in enumerate(items)]
        user = (f"[검색 맥락]\n검색어: {query}\n타깃 노드: {target_card}\n\n"
                f"[기사 목록]\n" + "\n".join(lines))
        try:
            out: PrefilterOutput = self.complete(
                system=get_prompt("harvester.prefilter"), user=user, schema=PrefilterOutput)
            return [i for i in out.relevant_indexes if 0 <= i < len(items)]
        except Exception as exc:                  # 필터 실패 → 전체 통과 (수확 보존)
            emit("prefilter_skip", error=str(exc)[:200], query=query)
            return list(range(len(items)))


# ───────────────────────────────────────────────────────────
# 스코프 이름 테이블 (결정적 연결의 기준)
# ───────────────────────────────────────────────────────────
_LINKABLE_LABELS = [
    L.COMMERCIAL_AREA, L.ADMIN_DONG, L.INDUSTRY, L.COMPLEX, L.BUILDING,
    L.DEVELOPMENT_PROJECT, L.TRANSIT_STATION, L.ENTITY, L.ASSET,
]


@dataclass(frozen=True)
class ScopeName:
    label: str
    key: str
    name: str
    norm: str


def load_scope_names(driver: Driver, asset_id: str, *, hops: int = 3) -> list[ScopeName]:
    """자산 스코프 노드들의 (라벨, 키, 이름) — mention 연결 판정 기준 테이블."""
    with driver.session() as s:
        result = s.run(
            f"""
            MATCH (a:{L.ASSET} {{asset_id: $aid}})
            MATCH (a)-[*0..{int(hops)}]-(n)
            WITH DISTINCT n, labels(n)[0] AS lbl
            WHERE lbl IN $linkable
            RETURN lbl AS label, n[$keymap[lbl]] AS key,
                   coalesce(n.trdar_name, n.adstrd_name, n.industry_name,
                            n.canonical_name, n.name, n.station_name,
                            n.building_name, n.bname) AS name
            """,
            aid=asset_id, linkable=_LINKABLE_LABELS, keymap=KEY_PROP,
        )
        out = []
        for r in result:
            if r["name"]:
                out.append(ScopeName(r["label"], str(r["key"]), r["name"], _norm(r["name"])))
        return out


def _asset_sigungu(driver: Driver, asset_id: str) -> str | None:
    with driver.session() as s:
        r = s.run(f"MATCH (a:{L.ASSET} {{asset_id: $a}}) RETURN a.sigungu AS sg",
                  a=asset_id).single()
        return r["sg"] if r else None


def store_extracted_facts(
    sg, edges, facts, *, scope, asset_sigungu, source_label, source_key,
    source_tier, published_at, collected_by,
) -> tuple[dict, list[str]]:
    """Fact 노드 + EXTRACTED_FROM + MENTIONS/Entity 연결 (D9). 뉴스·UserDoc 공용.

    반환: ({facts, entities, confirmed, candidate}, fact_texts).
    """
    counts = {"facts": 0, "entities": 0, "confirmed": 0, "candidate": 0}
    texts: list[str] = []
    for fd in facts:
        fact_id = _short_hash(_norm(fd.text))
        confidence = round(source_tier * max(0.0, min(1.0, fd.extraction_confidence)), 3)
        sg.add_node(L.FACT, fact_id, {
            "text": fd.text, "published_at": published_at,
            "event_date": fd.event_date, "valid_until": fd.valid_until,
            "temporal_kind": fd.temporal_kind,
            "extraction_confidence": fd.extraction_confidence,
            "source_tier": source_tier, "confidence": confidence,
            "collected_by": collected_by,
        })
        counts["facts"] += 1
        texts.append(fd.text)
        edges.append(SeedEdge(L.FACT, fact_id, E.EXTRACTED_FROM, source_label, source_key))
        for m in fd.mentions:
            kind, sn = link_mention(m, scope, asset_sigungu)
            if kind == "mentions":
                edges.append(SeedEdge(L.FACT, fact_id, E.MENTIONS, sn.label, sn.key))
                counts["confirmed"] += 1
                continue
            entity_id = _norm(m.surface)
            sg.add_node(L.ENTITY, entity_id, {
                "name": m.surface, "guess_label": m.guess_label,
                "resolution_status": "unresolved", "collected_by": collected_by})
            counts["entities"] += 1
            edges.append(SeedEdge(L.FACT, fact_id, E.MENTIONS, L.ENTITY, entity_id))
            if kind == "candidate" and sn is not None:
                edges.append(SeedEdge(L.ENTITY, entity_id, E.SAME_AS_CANDIDATE,
                                      sn.label, sn.key,
                                      {"surface": m.surface, "matched_name": sn.name}))
                counts["candidate"] += 1
    return counts, texts


def link_mention(
    m: MentionDraft,
    scope: list[ScopeName],
    asset_sigungu: str | None,
) -> tuple[str, ScopeName | None]:
    """결정적 연결 판정 (D9). 반환: (kind, 대상)
        kind = 'mentions'  → 스코프 노드에 확정 연결
               'candidate' → Entity 신설 + 유사 노드에 SAME_AS_CANDIDATE
               'entity'    → Entity 단독
    """
    surface_norm = _norm(m.surface)
    if not surface_norm:
        return "entity", None
    # 시군구 단서가 자산 시군구와 모순 → 동명 타지역 위험, 연결 금지
    if m.sigungu and asset_sigungu and _norm(m.sigungu) != _norm(asset_sigungu):
        return "entity", None
    for sn in scope:                          # ① 정규화 정확 일치 → 확정
        if surface_norm == sn.norm:
            return "mentions", sn
    shorter_ok = lambda a, b: min(len(a), len(b)) >= 3   # '강남'(2자) 오매칭 방지
    for sn in scope:                          # ② 포함 관계 → 미확정 후보
        if shorter_ok(surface_norm, sn.norm) and (
            surface_norm in sn.norm or sn.norm in surface_norm
        ):
            return "candidate", sn
    return "entity", None                     # ③ 불일치 → 단독 Entity


# ───────────────────────────────────────────────────────────
# Harvester 에이전트
# ───────────────────────────────────────────────────────────
_NAVER_HOSTS = ("news.naver.com", "n.news.naver.com", "m.news.naver.com")


def _broaden_query(query: str) -> str | None:
    """0건 쿼리 축약 — 앞 2어절(보통 고유명사+핵심어)만 남긴다. 이미 짧으면 None.

    navigator가 짧게 만들도록 고쳤지만(_MAX_QUERY_WORDS), 그래도 0건이 남으면
    마지막 안전망. 키워드 검색은 토큰이 적을수록 매칭 확률이 높다.
    """
    toks = query.split()
    if len(toks) <= 2:
        return None
    bq = " ".join(toks[:2])
    return bq if bq != query else None


def _make_web_search():
    """Tavily 웹검색 도구 (TAVILY_API_KEY 있을 때만, 없으면 None → graceful skip)."""
    import os
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return None
    try:
        from infra.tool.web import TavilyWebSearchTool
        return TavilyWebSearchTool(key)
    except Exception as exc:                  # 의존성/초기화 실패 → 네이버만
        logger.warning("Tavily 초기화 실패 — 네이버만 사용: %s", exc)
        return None


def _existing_doc_hashes(driver: Driver, hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    with driver.session() as s:
        result = s.run(
            f"UNWIND $hs AS h MATCH (d:{L.WEB_DOC} {{url_hash: h}}) RETURN h",
            hs=hashes)
        return {r["h"] for r in result}


@dataclass
class HarvestStats:
    queries: int = 0
    docs_new: int = 0
    facts_new: int = 0
    entities_new: int = 0
    mentions_confirmed: int = 0
    mentions_candidate: int = 0
    new_fact_texts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.new_fact_texts is None:
            self.new_fact_texts = []


class Harvester(BaseAgent):
    name = "harvester"

    def __init__(self):
        super().__init__()
        self._search = NaverNewsSearchTool()
        self._scrape = NaverNewsScrapeTool()
        self._vertical = NaverVerticalSearchTool()   # blog/cafe/web 스니펫(스크랩 불필요)
        self._prefilter = _Prefilter()
        self._web = _make_web_search()       # Tavily (키 있으면) — 멀티소스 보강

    def harvest(
        self,
        driver: Driver,
        *,
        asset_id: str,
        asset_card: str,
        asset_sigungu: str | None,
        plans: list,                       # navigator.QueryPlan (target ref + queries)
        query_hashes: dict[str, str],      # {query_text: query_hash}
        target_cards: dict[str, str],      # {ref: card_text}
        collected_by: str,
        scope_hops: int = 3,
    ) -> HarvestStats:
        """확장 계획 실행 — 검색→스크랩→추출→연결→적재. 라운드 1회 분량."""
        stats = HarvestStats()
        scope = load_scope_names(driver, asset_id, hops=scope_hops)

        for plan in plans:
            target_label, target_key = plan.target.split(":", 1)
            target_card = target_cards.get(plan.target, "")
            for q in plan.queries:
                stats.queries += 1
                self._harvest_one_query(
                    driver, stats, scope,
                    query=q, query_hash=query_hashes[q],
                    target_label=target_label, target_key=target_key,
                    target_card=target_card, asset_card=asset_card,
                    asset_sigungu=asset_sigungu, collected_by=collected_by,
                )
        return stats

    # ── 쿼리 1건 처리 ──────────────────────────────────────
    def _harvest_one_query(self, driver, stats, scope, *, query, query_hash,
                           target_label, target_key, target_card,
                           asset_card, asset_sigungu, collected_by) -> None:
        try:
            found = self._search(query=query, display=10, sort="sim")
            if found["n_results"] == 0:
                # 0건 폴백 — 앞 핵심어로 축약해 1회 재시도 (마지막 안전망).
                bq = _broaden_query(query)
                if bq:
                    f2 = self._search(query=bq, display=10, sort="sim")
                    if f2["n_results"] > 0:
                        emit("harvest_broaden", original=query[:60], broadened=bq,
                             n=f2["n_results"])
                        found, query = f2, bq
        except Exception as exc:
            emit("harvest_skip", reason="search_error", query=query, error=str(exc)[:200])
            return

        # 네이버 뉴스 호스트만 (본문 스크랩 가능한 것)
        items = [it for it in found["items"]
                 if any(h in (it.get("link") or "") for h in _NAVER_HOSTS)]
        with driver.session() as s:
            s.run(f"MATCH (q:{L.QUERY} {{query_hash: $qh}}) SET q.n_results = $n",
                  qh=query_hash, n=found["n_results"])

        # 사전 필터 — 제목+요약으로 관련 기사만 (스크랩·추출 비용 절감)
        if items:
            keep = self._prefilter.select(query=query, target_card=target_card, items=items)
            emit("prefilter", query=query, before=len(items), after=len(keep))
            items = [items[i] for i in keep]

        hashes = [_short_hash(it["link"]) for it in items]
        existing = _existing_doc_hashes(driver, hashes)

        scraped = 0
        for it, url_hash in zip(items, hashes):
            if scraped >= _MAX_ARTICLES_PER_QUERY:
                break
            if url_hash in existing:
                # 기수확 문서 — 새 Query에서 RETRIEVED 엣지만 연결 (재추출 안 함)
                write_edges(driver, [SeedEdge(L.QUERY, query_hash, E.RETRIEVED,
                                               L.WEB_DOC, url_hash)])
                continue
            art = self._scrape(url=it["link"])
            if not art.get("ok"):
                continue
            scraped += 1
            self._extract_and_store(
                driver, stats, scope,
                article=art, meta=it, url_hash=url_hash, query=query,
                query_hash=query_hash, target_label=target_label,
                target_key=target_key, target_card=target_card,
                asset_card=asset_card, asset_sigungu=asset_sigungu,
                collected_by=collected_by,
            )

        # 멀티소스 보강 — 스니펫 채널(스크랩 불필요). 네이버 뉴스가 못 보는
        # 모달리티(블로그·카페·웹문서·실거래·지역 글)를 메운다. description/content
        # 인라인 스니펫을 그대로 문서 본문으로 사용. 채널별 신뢰 prior(_SOURCE_TIER)로
        # down-weight되므로 저신뢰 채널도 가산적으로 수집.
        self._harvest_naver_verticals(
            driver, stats, scope, query=query, query_hash=query_hash,
            target_label=target_label, target_key=target_key,
            target_card=target_card, asset_card=asset_card,
            asset_sigungu=asset_sigungu, collected_by=collected_by)
        if self._web is not None:                 # Tavily 일반 웹(키 있을 때)
            self._harvest_web(
                driver, stats, scope, query=query, query_hash=query_hash,
                target_label=target_label, target_key=target_key,
                target_card=target_card, asset_card=asset_card,
                asset_sigungu=asset_sigungu, collected_by=collected_by)

    # ── 일반 웹검색(Tavily) 1쿼리: content 인라인 → 스크랩 없이 추출 ──
    def _harvest_web(self, driver, stats, scope, *, query, query_hash,
                     target_label, target_key, target_card,
                     asset_card, asset_sigungu, collected_by) -> None:
        try:
            # topic='general' — 일반 웹(블로그·실거래·상권·지역 글). 'news'는 한국어
            # 쿼리에 무관한 영어 뉴스를 반환해 역효과(네이버 뉴스가 뉴스 모달리티 담당).
            found = self._web(query=query, max_results=6, topic="general")
        except Exception as exc:
            emit("harvest_skip", reason="web_search_error", query=query, error=str(exc)[:200])
            return
        # Tavily 결과 → 공통 스니펫 article shape (content가 본문).
        docs = [
            {"title": it.get("title"), "url": it.get("link"),
             "press": it.get("source"), "body": it.get("content"),
             "published_at": it.get("pub_date"),
             "snippet": (it.get("content") or "")[:200]}
            for it in found["items"] if (it.get("content") or "").strip()
        ]
        self._ingest_snippet_docs(
            driver, stats, scope, docs=docs, source_tier=SOURCE_TIER_WEB,
            emit_label="web", query=query, query_hash=query_hash,
            target_label=target_label, target_key=target_key, target_card=target_card,
            asset_card=asset_card, asset_sigungu=asset_sigungu, collected_by=collected_by)

    # ── 네이버 비뉴스 vertical(blog/cafe/webkr) 1쿼리: 스니펫 인라인 → 스크랩 없이 추출 ──
    def _harvest_naver_verticals(self, driver, stats, scope, *, query, query_hash,
                                 target_label, target_key, target_card,
                                 asset_card, asset_sigungu, collected_by) -> None:
        for vertical in _SNIPPET_VERTICALS:
            try:
                found = self._vertical(query=query, vertical=vertical, display=10, sort="sim")
            except Exception as exc:
                emit("harvest_skip", reason="vertical_search_error", vertical=vertical,
                     query=query, error=str(exc)[:200])
                continue
            # description(스니펫)이 곧 본문. 출처명은 press 자리에 표기.
            docs = [
                {"title": it.get("title"), "url": it.get("link"),
                 "press": it.get("source_name"), "body": it.get("description"),
                 "published_at": it.get("posted_at"),
                 "snippet": (it.get("description") or "")[:200]}
                for it in found["items"] if (it.get("description") or "").strip()
            ]
            self._ingest_snippet_docs(
                driver, stats, scope, docs=docs,
                source_tier=_SOURCE_TIER.get(vertical, SOURCE_TIER_FORUM),
                emit_label=f"naver_{vertical}", query=query, query_hash=query_hash,
                target_label=target_label, target_key=target_key, target_card=target_card,
                asset_card=asset_card, asset_sigungu=asset_sigungu,
                collected_by=collected_by)

    # ── 스니펫 문서 묶음 적재 (Tavily·네이버 vertical 공용) ──
    # 스크랩 불필요한 소스의 공통 골격: prefilter → 중복 dedup → 추출/적재.
    # article 은 {title,url,press,body,published_at,snippet} (snippet=prefilter용 요약).
    def _ingest_snippet_docs(self, driver, stats, scope, *, docs, source_tier,
                             emit_label, query, query_hash, target_label, target_key,
                             target_card, asset_card, asset_sigungu, collected_by) -> None:
        if not docs:
            return
        pf = [{"title": d["title"], "description": d["snippet"]} for d in docs]
        keep = self._prefilter.select(query=query, target_card=target_card, items=pf)
        emit(f"prefilter_{emit_label}", query=query, before=len(docs), after=len(keep))
        docs = [docs[i] for i in keep]
        if not docs:
            return

        hashes = [_short_hash(d["url"]) for d in docs]
        existing = _existing_doc_hashes(driver, hashes)
        stored = 0
        for d, url_hash in zip(docs, hashes):
            if stored >= _MAX_ARTICLES_PER_QUERY:
                break
            if url_hash in existing:
                write_edges(driver, [SeedEdge(L.QUERY, query_hash, E.RETRIEVED,
                                               L.WEB_DOC, url_hash)])
                continue
            stored += 1
            article = {"title": d["title"], "url": d["url"], "press": d["press"],
                       "body": d["body"], "published_at": d["published_at"], "ok": True}
            self._extract_and_store(
                driver, stats, scope, article=article, meta=d, url_hash=url_hash,
                query=query, query_hash=query_hash, target_label=target_label,
                target_key=target_key, target_card=target_card,
                asset_card=asset_card, asset_sigungu=asset_sigungu,
                collected_by=collected_by, source_tier=source_tier)

    # ── 기사 1건: 추출 + 적재 ───────────────────────────────
    def _extract_and_store(self, driver, stats, scope, *, article, meta, url_hash,
                           query, query_hash, target_label, target_key, target_card,
                           asset_card, asset_sigungu, collected_by,
                           source_tier=SOURCE_TIER_NEWS) -> None:
        published_at = article.get("published_at") or meta.get("pub_date") or ""
        body = (article.get("body") or "")[:_BODY_TRIM]
        user = (
            f"[평가 대상 자산]\n{asset_card}\n\n"
            f"[검색 맥락]\n검색어: {query}\n타깃 노드: {target_card}\n\n"
            f"[기사]\n제목: {article.get('title')}\n언론사: {article.get('press')}\n"
            f"보도일: {published_at}\n본문:\n{body}"
        )
        try:
            out: ExtractionOutput = self.complete(system=get_prompt("harvester.system"), user=user,
                                                  schema=ExtractionOutput)
        except Exception as exc:
            emit("harvest_skip", reason="extract_error", url=article.get("url"),
                 error=str(exc)[:200])
            return

        sg = ScopeGraph()
        edges: list[SeedEdge] = []

        doc = sg.add_node(L.WEB_DOC, url_hash, {
            "title": article.get("title"), "url": article.get("url"),
            "press": article.get("press"), "published_at": published_at,
            "source_tier": source_tier,
            "doc_relevance": out.doc_relevance,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "collected_by": collected_by,
        })
        stats.docs_new += 1
        edges.append(SeedEdge(L.QUERY, query_hash, E.RETRIEVED, L.WEB_DOC, url_hash))
        edges.append(SeedEdge(L.WEB_DOC, url_hash, E.ABOUT, target_label, target_key))

        counts, texts = store_extracted_facts(
            sg, edges, out.facts[:_MAX_FACTS_PER_DOC], scope=scope,
            asset_sigungu=asset_sigungu, source_label=L.WEB_DOC, source_key=url_hash,
            source_tier=source_tier, published_at=published_at,
            collected_by=collected_by)
        stats.facts_new += counts["facts"]
        stats.entities_new += counts["entities"]
        stats.mentions_confirmed += counts["confirmed"]
        stats.mentions_candidate += counts["candidate"]
        stats.new_fact_texts.extend(texts)

        seed(sg, driver)                # 신규 노드 MERGE (+카드)
        write_edges(driver, edges)     # 기존 노드 향한 엣지 포함
        emit("harvest_doc", url=article.get("url"), facts=len(out.facts),
             doc_relevance=out.doc_relevance, query=query)

    # ── 발행자 첨부 자료 인입 (§10.5, D14) ─────────────────
    def ingest_user_doc(self, driver, *, asset_id, text, provider,
                        collected_by, context="") -> list[str]:
        """발행자 제공 자료 → UserDoc(provisional tier) + Fact 추출/연결.

        '의뢰자가 준 자료'라는 이유로 자동 신뢰하지 않는다 — 일반 증거와 동일
        파이프라인(추출→연결 D9→confidence→validity)을 통과하되 source_tier만
        provisional(0.5)로 낮게 시작. 반환: 추출된 Fact 텍스트 목록.
        """
        scope = load_scope_names(driver, asset_id)
        asset_sigungu = _asset_sigungu(driver, asset_id)
        doc_hash = _short_hash(text[:1000] + provider)
        user = (f"[발행자 제공 자료]\n출처: {provider}\n{context}\n\n"
                f"본문:\n{text[:_BODY_TRIM]}")
        try:
            out: ExtractionOutput = self.complete(system=get_prompt("harvester.system"), user=user,
                                                  schema=ExtractionOutput)
        except Exception as exc:
            emit("userdoc_extract_error", error=str(exc)[:200])
            return []
        sg = ScopeGraph()
        edges: list[SeedEdge] = []
        now = datetime.now().isoformat(timespec="seconds")
        sg.add_node(L.USER_DOC, doc_hash, {
            "provider": provider, "source_tier": SOURCE_TIER_PROVISIONAL,
            "received_at": now, "doc_relevance": out.doc_relevance,
            "collected_by": collected_by})
        counts, texts = store_extracted_facts(
            sg, edges, out.facts[:_MAX_FACTS_PER_DOC], scope=scope,
            asset_sigungu=asset_sigungu, source_label=L.USER_DOC, source_key=doc_hash,
            source_tier=SOURCE_TIER_PROVISIONAL, published_at=now[:10],
            collected_by=collected_by)
        seed(sg, driver)
        write_edges(driver, edges)
        # 신규 첨부 Fact 임베딩 — assess의 벡터 검색에 잡히도록
        from real_estate.graphflow.graph.vector_index import (
            ensure_vector_index,
            update_embeddings_for_label,
        )
        ensure_vector_index(driver)
        for label in (L.FACT, L.ENTITY, L.USER_DOC):
            update_embeddings_for_label(driver, label)
        emit("userdoc_ingested", provider=provider, facts=counts["facts"],
             confirmed=counts["confirmed"], candidate=counts["candidate"])
        return texts
