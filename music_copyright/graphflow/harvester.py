"""Harvester (음악저작권) — 웹검색 실행 + Fact 추출 + 그래프 적재. real_estate 미러(경량판).

real_estate는 네이버 뉴스 스크랩 + prefilter + 복잡한 주소 매칭을 한다. 음악은
구조화 DB가 없고 평가 대상이 곡/아티스트라, 검색 스니펫(content/description)을 그대로
본문으로 써 스크랩을 생략한 경량 harvester다.

채널(가용한 것만 best-effort): ddgs(무키) · Tavily(키) · 네이버 뉴스(크리덴셜).
LLM(music_harvester, gpt_mini)이 문서에서 원자적 Fact를 추출하고, 코드가 mention을
스코프 노드(Work/Artist/Platform/…)에 결정적으로 링크한다.

적재: WebDoc + Fact + (Query)-RETRIEVED->(WebDoc) + (WebDoc)-ABOUT->(target) +
      (Fact)-EXTRACTED_FROM->(WebDoc) + (Fact)-MENTIONS->(노드|Entity).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal

from neo4j import Driver
from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import (
    BaseAgent, music_system_prompt, normalize_text, short_hash)
from music_copyright.graphflow.graph.cards import build_card
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.navigator import QueryPlan
from music_copyright.graphflow.prompts import get_prompt
from music_copyright.graphflow.traversal import scope_eids

logger = logging.getLogger("music_copyright.harvester")

_MAX_DOCS_PER_QUERY = 5
_MAX_FACTS_PER_DOC = 6
_BODY_TRIM = 3000
_MIN_DOC_RELEVANCE = 0.3

# 출처 신뢰 prior (real_estate _SOURCE_TIER 음악판)
_TIER_NEWS, _TIER_WEB, _TIER_BLOG = 0.8, 0.68, 0.56


# ───────────────────────────────────────────────────────────
# LLM 추출 스키마
# ───────────────────────────────────────────────────────────
class MentionDraft(BaseModel):
    surface: str = Field(description="문서에 나온 표기 그대로 (곡명·아티스트·차트·수상명 등)")
    guess_label: str | None = Field(default=None,
        description="추정 종류: Work/Artist/Chart/Award/Rightsholder/Platform 또는 null")


class FactDraft(BaseModel):
    text: str = Field(description="원자적 사실 1문장 (한국어). 숫자·순위·날짜는 문서 표기 그대로")
    mentions: list[MentionDraft] = Field(default_factory=list)
    event_date: str | None = Field(default=None, description="YYYY-MM(-DD) 또는 null")
    extraction_confidence: float = Field(default=0.6, description="추출 확신도 0~1")


class ExtractionOutput(BaseModel):
    facts: list[FactDraft] = Field(default_factory=list)
    doc_relevance: float = Field(default=0.5, description="문서의 검색맥락 관련도 0~1")


@dataclass
class HarvestStats:
    queries: int = 0
    docs_new: int = 0
    facts_new: int = 0
    entities_new: int = 0
    mentions_confirmed: int = 0
    new_fact_texts: list[str] = field(default_factory=list)


# ───────────────────────────────────────────────────────────
# 스코프 이름 로딩 (mention 링크용)
# ───────────────────────────────────────────────────────────
def _load_scope_names(driver: Driver, work_id: str, scope_hops: int) -> dict[str, tuple[str, str]]:
    """Work 스코프 내 노드 이름 → (label, key). mention surface 매칭에 사용."""
    eids = scope_eids(driver, work_id, scope_hops)
    if not eids:
        return {}
    out: dict[str, tuple[str, str]] = {}
    with driver.session() as s:
        rows = s.run(
            "UNWIND $eids AS eid MATCH (n) WHERE elementId(n)=eid "
            "RETURN labels(n)[0] AS label, properties(n) AS p", eids=list(eids),
        ).data()
    for r in rows:
        label, p = r["label"], r["p"] or {}
        key = p.get(KEY_PROP.get(label, ""))
        if not key:
            continue
        for nk in ("title", "artist_name", "name", "chart_name", "award_name"):
            v = p.get(nk)
            if v and isinstance(v, str):
                out.setdefault(normalize_text(v), (label, str(key)))
    return out


def _source_tier(source: str | None, channel: str) -> float:
    if channel == "news":
        return _TIER_NEWS
    s = (source or "").lower()
    if any(b in s for b in ("blog", "tistory", "cafe", "namu")):
        return _TIER_BLOG
    return _TIER_WEB


# ───────────────────────────────────────────────────────────
# Harvester
# ───────────────────────────────────────────────────────────
class Harvester(BaseAgent):
    name = "music_harvester"

    def __init__(self):
        super().__init__()
        from infra.tool.ddgs import DdgsSearchTool
        self._ddgs = DdgsSearchTool()
        self._tavily = None
        tav_key = os.environ.get("TAVILY_API_KEY")
        if tav_key:
            from infra.tool.web import TavilyWebSearchTool
            self._tavily = TavilyWebSearchTool(tav_key)
        self._naver = None
        if os.environ.get("NAVER_CLIENT_ID"):
            from infra.tool.naver import NaverNewsSearchTool
            self._naver = NaverNewsSearchTool()

    # ── 검색 (채널 통합) ─────────────────────────────────────
    def _search(self, query: str) -> list[dict]:
        docs: list[dict] = []
        for topic, channel in (("text", "web"), ("news", "news")):
            try:
                res = self._ddgs(query=query, max_results=5, topic=topic)
                for it in res.get("items", []):
                    docs.append(self._doc(it, channel))
            except Exception as exc:  # noqa: BLE001
                logger.debug("ddgs(%s) skip: %s", topic, exc)
        if self._tavily:
            try:
                res = self._tavily(query=query, max_results=5, topic="news")
                for it in res.get("items", []):
                    docs.append(self._doc(it, "news"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("tavily skip: %s", exc)
        if self._naver:
            try:
                res = self._naver(query=query, display=8, sort="sim")
                for it in res.get("items", []):
                    docs.append(self._doc(
                        {"title": it.get("title"), "link": it.get("link"),
                         "content": it.get("description"), "pub_date": it.get("pub_date"),
                         "source": "naver"}, "news"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("naver skip: %s", exc)
        # url 중복 제거 + content 있는 것만
        seen, uniq = set(), []
        for d in docs:
            if d["url"] and d["url"] not in seen and (d["content"] or "").strip():
                seen.add(d["url"])
                uniq.append(d)
        return uniq[:_MAX_DOCS_PER_QUERY]

    @staticmethod
    def _doc(it: dict, channel: str) -> dict:
        return {
            "title": it.get("title") or "", "url": it.get("link") or "",
            "content": it.get("content") or "", "source": it.get("source"),
            "published_at": it.get("pub_date") or "", "channel": channel,
        }

    # ── 메인 ─────────────────────────────────────────────────
    def harvest(
        self,
        driver: Driver,
        *,
        work_id: str,
        work_card: str,
        plans: list[QueryPlan],
        query_hashes: dict[str, str],
        target_cards: dict[str, str],
        collected_by: str,
        scope_hops: int = 3,
    ) -> HarvestStats:
        stats = HarvestStats()
        scope_names = _load_scope_names(driver, work_id, scope_hops)
        for plan in plans:
            t_label, _, t_key = plan.target.partition(":")
            for query in plan.queries:
                stats.queries += 1
                docs = self._search(query)
                qhash = query_hashes.get(query) or short_hash(normalize_text(query))
                for d in docs:
                    self._ingest_doc(driver, d, query, qhash, t_label, t_key,
                                     work_card, scope_names, collected_by, stats)
        logger.info("music_harvest: q=%d docs=%d facts=%d ent=%d ment=%d",
                    stats.queries, stats.docs_new, stats.facts_new,
                    stats.entities_new, stats.mentions_confirmed)
        return stats

    def _ingest_doc(self, driver, d, query, qhash, t_label, t_key, work_card,
                    scope_names, collected_by, stats):
        # 1. Fact 추출 (LLM)
        try:
            out: ExtractionOutput = self.complete(
                system=music_system_prompt(get_prompt("music_harvester.system")),
                user=(f"[평가 대상]\n{work_card}\n\n[검색어]\n{query}\n\n"
                      f"[문서] {d['title']}\n{d['content'][:_BODY_TRIM]}"),
                schema=ExtractionOutput)
        except Exception as exc:  # noqa: BLE001
            logger.debug("extract skip: %s", exc)
            return
        if out.doc_relevance < _MIN_DOC_RELEVANCE or not out.facts:
            return

        tier = _source_tier(d["source"], d["channel"])
        url_hash = short_hash(d["url"])
        with driver.session() as s:
            # WebDoc + RETRIEVED + ABOUT(target)
            _merge_node(s, L.WEB_DOC, url_hash, {
                "title": d["title"], "url": d["url"], "source": d["source"],
                "published_at": d["published_at"], "source_tier": tier,
                "collected_by": collected_by})
            stats.docs_new += 1
            _merge_edge(s, L.QUERY, qhash, E.RETRIEVED, L.WEB_DOC, url_hash)
            if t_key:
                _merge_edge(s, L.WEB_DOC, url_hash, E.ABOUT, t_label, t_key)

            for fd in out.facts[:_MAX_FACTS_PER_DOC]:
                if not fd.text.strip():
                    continue
                fact_id = short_hash(normalize_text(fd.text))
                conf = round(min(1.0, tier * float(fd.extraction_confidence or 0.6)), 3)
                _merge_node(s, L.FACT, fact_id, {
                    "text": fd.text, "published_at": d["published_at"] or None,
                    "event_date": fd.event_date, "confidence": conf,
                    "source_tier": tier, "collected_by": collected_by})
                stats.facts_new += 1
                stats.new_fact_texts.append(fd.text)
                _merge_edge(s, L.FACT, fact_id, E.EXTRACTED_FROM, L.WEB_DOC, url_hash)
                self._link_mentions(s, fact_id, fd.mentions, scope_names,
                                    collected_by, stats)

    def _link_mentions(self, s, fact_id, mentions, scope_names, collected_by, stats):
        for m in mentions:
            nm = normalize_text(m.surface)
            if not nm:
                continue
            matched = scope_names.get(nm)
            if matched is None:                       # 부분 포함 매칭
                for name, lk in scope_names.items():
                    if len(name) >= 2 and (name in nm or nm in name):
                        matched = lk
                        break
            if matched:
                label, key = matched
                _merge_edge(s, L.FACT, fact_id, E.MENTIONS, label, key)
                stats.mentions_confirmed += 1
            else:                                     # 미해소 → Entity
                eid = short_hash(nm)
                _merge_node(s, L.ENTITY, eid, {
                    "name": m.surface, "guess_label": m.guess_label,
                    "collected_by": collected_by})
                stats.entities_new += 1
                _merge_edge(s, L.FACT, fact_id, E.MENTIONS, L.ENTITY, eid)


# ───────────────────────────────────────────────────────────
# 적재 헬퍼 (단건 MERGE)
# ───────────────────────────────────────────────────────────
def _merge_node(session, label: str, key: str, props: dict) -> None:
    key_prop = KEY_PROP[label]
    clean = {k: v for k, v in props.items() if v is not None and v != ""}
    clean[key_prop] = key
    card = build_card(label, clean)
    session.run(
        f"MERGE (n:{label} {{{key_prop}: $key}}) SET n += $props, n.card_text = $card",
        key=key, props={k: v for k, v in clean.items() if k != key_prop}, card=card)


def _merge_edge(session, src_label, src_key, rel, dst_label, dst_key, props=None) -> None:
    sk, dk = KEY_PROP[src_label], KEY_PROP[dst_label]
    session.run(
        f"MATCH (s:{src_label} {{{sk}: $src}}) MATCH (d:{dst_label} {{{dk}: $dst}}) "
        f"MERGE (s)-[r:{rel}]->(d) SET r += $props",
        src=src_key, dst=dst_key, props=props or {})
