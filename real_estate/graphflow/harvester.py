"""Harvester — 검색 실행 → WebDoc 적재 → Fact/Entity 추출 → 그래프 연결 (설계 §9).

오염 방지(D9)의 구현 지점: **LLM은 연결을 결정하지 않는다.** LLM은 mentions에
표면형(surface)+추정만 제시하고, 연결 판정은 이 모듈의 결정적 코드가 한다:
    - 정규화 이름이 스코프 노드와 정확히 일치          → MENTIONS (확정)
    - 포함 관계 등 이름 유사뿐                         → Entity 신설 + SAME_AS_CANDIDATE
    - 불일치 / code_clues(시군구) 모순                 → Entity 단독 (MENTIONS→Entity)

confidence = source_tier × extraction_confidence (§9.3). PR-C는 뉴스만이라
source_tier=0.8 고정 (공공 1.0/블로그 0.4는 채널 추가 시).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from neo4j import Driver
from pydantic import BaseModel, Field

from real_estate.graphflow.agents.base import BaseAgent
from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import KEY_PROP
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.seeder import ScopeGraph, SeedEdge, _write_edges, seed
from real_estate.graphflow.tool.naver import NaverNewsScrapeTool, NaverNewsSearchTool
from real_estate.graphflow.tracing import emit

logger = logging.getLogger("graphflow.harvester")

SOURCE_TIER_NEWS = 0.8          # 주요 언론 (§9.3). 공공 1.0 / 블로그 0.4은 채널 추가 시
_MAX_ARTICLES_PER_QUERY = 3     # 쿼리당 본문 스크랩·추출 상한 (LLM 비용 캡)
_MAX_FACTS_PER_DOC = 5
_BODY_TRIM = 3500               # 추출 프롬프트에 넣는 본문 길이 상한

_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS_RE.sub("", (s or "").strip().lower())


def _short_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


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


_SYSTEM = (
    "당신은 상가 부동산 평가를 위한 사실 추출가(Harvester)입니다.\n"
    "뉴스 기사에서 '검색 맥락(자산/타깃 노드)'의 평가에 기여할 원자적 사실을 "
    "1문장 단위로 추출합니다.\n"
    "규칙:\n"
    "- 기사에 명시된 내용만 추출합니다. 해석/추측/일반론 금지.\n"
    "- 한 사실에 한 문장. 숫자·날짜는 기사 표기 그대로.\n"
    "- 검색 맥락과 무관한 사실(연예/사건사고 등)은 버립니다. 전부 무관하면 빈 배열.\n"
    "- mentions의 surface는 기사 표기 그대로 적습니다 — 어떤 노드와 연결할지는 "
    "시스템이 판정하므로 임의로 정규화하지 마세요.\n"
    "- 원문에 '~까지', '~예정', '한시' 등 유효기간이 명시되면 valid_until로 추출합니다."
)


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
        except Exception as exc:
            emit("harvest_skip", reason="search_error", query=query, error=str(exc)[:200])
            return

        # 네이버 뉴스 호스트만 (본문 스크랩 가능한 것)
        items = [it for it in found["items"]
                 if any(h in (it.get("link") or "") for h in _NAVER_HOSTS)]
        with driver.session() as s:
            s.run(f"MATCH (q:{L.QUERY} {{query_hash: $qh}}) SET q.n_results = $n",
                  qh=query_hash, n=found["n_results"])

        hashes = [_short_hash(it["link"]) for it in items]
        existing = _existing_doc_hashes(driver, hashes)

        scraped = 0
        for it, url_hash in zip(items, hashes):
            if scraped >= _MAX_ARTICLES_PER_QUERY:
                break
            if url_hash in existing:
                # 기수확 문서 — 새 Query에서 RETRIEVED 엣지만 연결 (재추출 안 함)
                _write_edges(driver, [SeedEdge(L.QUERY, query_hash, E.RETRIEVED,
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

    # ── 기사 1건: 추출 + 적재 ───────────────────────────────
    def _extract_and_store(self, driver, stats, scope, *, article, meta, url_hash,
                           query, query_hash, target_label, target_key, target_card,
                           asset_card, asset_sigungu, collected_by) -> None:
        published_at = article.get("published_at") or meta.get("pub_date") or ""
        body = (article.get("body") or "")[:_BODY_TRIM]
        user = (
            f"[평가 대상 자산]\n{asset_card}\n\n"
            f"[검색 맥락]\n검색어: {query}\n타깃 노드: {target_card}\n\n"
            f"[기사]\n제목: {article.get('title')}\n언론사: {article.get('press')}\n"
            f"보도일: {published_at}\n본문:\n{body}"
        )
        try:
            out: ExtractionOutput = self.complete(system=_SYSTEM, user=user,
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
            "source_tier": SOURCE_TIER_NEWS,
            "doc_relevance": out.doc_relevance,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "collected_by": collected_by,
        })
        stats.docs_new += 1
        edges.append(SeedEdge(L.QUERY, query_hash, E.RETRIEVED, L.WEB_DOC, url_hash))
        edges.append(SeedEdge(L.WEB_DOC, url_hash, E.ABOUT, target_label, target_key))

        for fd in out.facts[:_MAX_FACTS_PER_DOC]:
            fact_id = _short_hash(_norm(fd.text))
            confidence = round(SOURCE_TIER_NEWS * max(0.0, min(1.0, fd.extraction_confidence)), 3)
            sg.add_node(L.FACT, fact_id, {
                "text": fd.text, "published_at": published_at,
                "event_date": fd.event_date, "valid_until": fd.valid_until,
                "temporal_kind": fd.temporal_kind,
                "extraction_confidence": fd.extraction_confidence,
                "source_tier": SOURCE_TIER_NEWS, "confidence": confidence,
                "collected_by": collected_by,
            })
            stats.facts_new += 1
            stats.new_fact_texts.append(fd.text)
            edges.append(SeedEdge(L.FACT, fact_id, E.EXTRACTED_FROM, L.WEB_DOC, url_hash))

            for m in fd.mentions:
                kind, sn = link_mention(m, scope, asset_sigungu)
                if kind == "mentions":
                    edges.append(SeedEdge(L.FACT, fact_id, E.MENTIONS, sn.label, sn.key))
                    stats.mentions_confirmed += 1
                    continue
                # Entity 신설 (candidate/entity 공통) — entity_id = 정규화 표면형
                entity_id = _norm(m.surface)
                sg.add_node(L.ENTITY, entity_id, {
                    "name": m.surface, "guess_label": m.guess_label,
                    "resolution_status": "unresolved",
                    "collected_by": collected_by,
                })
                stats.entities_new += 1
                edges.append(SeedEdge(L.FACT, fact_id, E.MENTIONS, L.ENTITY, entity_id))
                if kind == "candidate" and sn is not None:
                    edges.append(SeedEdge(L.ENTITY, entity_id, E.SAME_AS_CANDIDATE,
                                          sn.label, sn.key,
                                          {"surface": m.surface, "matched_name": sn.name}))
                    stats.mentions_candidate += 1

        seed(sg, driver)                # 신규 노드 MERGE (+카드)
        _write_edges(driver, edges)     # 기존 노드 향한 엣지 포함
        emit("harvest_doc", url=article.get("url"), facts=len(out.facts),
             doc_relevance=out.doc_relevance, query=query)
