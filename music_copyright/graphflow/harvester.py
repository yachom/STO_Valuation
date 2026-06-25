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

import contextvars
import logging
import os
from concurrent.futures import ThreadPoolExecutor
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
# 쿼리 내 문서별 LLM 추출(_ingest_doc)을 병렬화하는 스레드풀 동시성 상한.
# 문서별 gpt_mini Fact 추출 직렬 호출이 수집 wall-clock의 대부분이다 — 게이트
# throttle된 검색(_search)은 그대로 순차로 두고, 모은 문서들의 추출·적재만 동시 실행한다.
# 게이트웨이가 openai 호출을 전역 throttle(5/s)하므로 워커가 많아도 429가 없다.
_MAX_EXTRACT_WORKERS = 8

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


@dataclass
class _DocDeltas:
    """문서 1건 적재의 stats 증분 — 스레드 로컬로 모은 뒤 풀 종료 후 직렬 병합.

    각 _ingest_doc 태스크는 공유 HarvestStats를 직접 건드리지 않고 이 값을 반환한다.
    워커는 자기 _DocDeltas만 쓰므로 락이 불필요하고, 병합은 디스패치 스레드가 입력
    순서대로(map) 한 번에 한다(merge_into) — 누적 순서가 직렬 베이스라인과 동일.
    """
    docs_new: int = 0
    facts_new: int = 0
    entities_new: int = 0
    mentions_confirmed: int = 0
    new_fact_texts: list[str] = field(default_factory=list)

    def merge_into(self, stats: HarvestStats) -> None:
        stats.docs_new += self.docs_new
        stats.facts_new += self.facts_new
        stats.entities_new += self.entities_new
        stats.mentions_confirmed += self.mentions_confirmed
        stats.new_fact_texts.extend(self.new_fact_texts)


def _parallel_ingest(ingest_fn, tasks: list[dict]) -> list["_DocDeltas"]:
    """쿼리 내 문서별 적재(_ingest_doc)를 bounded 스레드풀로 병렬 실행 후 deltas 반환.

    ingest_fn(**task) -> _DocDeltas. 각 태스크는 자기 driver.session()으로 독립 적재하고
    stats 증분만 반환하므로 동시 실행이 안전하다(Neo4j driver는 스레드 안전, 세션은
    _ingest_doc이 호출마다 새로 연다). 트레이싱 contextvar(_current_node/_axis/_tracer)는
    워커 스레드로 자동 전파되지 않으므로 현재 컨텍스트를 복사해 각 태스크를 그 안에서
    실행한다(emit·토큰 roll-up·Langfuse span 스탬핑 보존). 문서 1건이면 풀 없이 인라인
    실행(오버헤드 회피). 반환은 입력 순서(pool.map)라 호출부 직렬 병합이 결정적이다.
    """
    results: list[_DocDeltas] = []
    if not tasks:
        return results
    if len(tasks) == 1:
        results.append(ingest_fn(**tasks[0]))
        return results
    workers = min(_MAX_EXTRACT_WORKERS, len(tasks))

    def _run_in_ctx(task: dict) -> _DocDeltas:
        ctx = contextvars.copy_context()
        return ctx.run(ingest_fn, **task)

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="music-harvest") as pool:
        for deltas in pool.map(_run_in_ctx, tasks):
            results.append(deltas)
    return results


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
                docs = self._search(query)          # 게이트 throttle된 검색 — 순차
                qhash = query_hashes.get(query) or short_hash(normalize_text(query))
                # 모은 문서들의 LLM 추출·적재만 스레드풀로 병렬(_parallel_ingest).
                tasks = [dict(driver=driver, d=d, query=query, qhash=qhash,
                              t_label=t_label, t_key=t_key, work_card=work_card,
                              scope_names=scope_names, collected_by=collected_by)
                         for d in docs]
                for deltas in _parallel_ingest(self._ingest_doc, tasks):
                    deltas.merge_into(stats)
        logger.info("music_harvest: q=%d docs=%d facts=%d ent=%d ment=%d",
                    stats.queries, stats.docs_new, stats.facts_new,
                    stats.entities_new, stats.mentions_confirmed)
        return stats

    # stats를 직접 갱신하지 않고 _DocDeltas를 반환한다 — 쿼리 내 문서들을 스레드풀로
    # 동시 추출·적재하기 위함(_parallel_ingest). 워커는 자기 driver.session()으로만
    # 적재하므로 공유 가변상태가 없고, 증분 병합은 디스패치 스레드가 직렬로 한다.
    def _ingest_doc(self, driver, d, query, qhash, t_label, t_key, work_card,
                    scope_names, collected_by) -> "_DocDeltas":
        deltas = _DocDeltas()
        # 1. Fact 추출 (LLM)
        try:
            out: ExtractionOutput = self.complete(
                system=music_system_prompt(get_prompt("music_harvester.system")),
                user=(f"[평가 대상]\n{work_card}\n\n[검색어]\n{query}\n\n"
                      f"[문서] {d['title']}\n{d['content'][:_BODY_TRIM]}"),
                schema=ExtractionOutput)
        except Exception as exc:  # noqa: BLE001
            logger.debug("extract skip: %s", exc)
            return deltas
        if out.doc_relevance < _MIN_DOC_RELEVANCE or not out.facts:
            return deltas

        tier = _source_tier(d["source"], d["channel"])
        url_hash = short_hash(d["url"])
        with driver.session() as s:
            # WebDoc + RETRIEVED + ABOUT(target)
            _merge_node(s, L.WEB_DOC, url_hash, {
                "title": d["title"], "url": d["url"], "source": d["source"],
                "published_at": d["published_at"], "source_tier": tier,
                "collected_by": collected_by})
            deltas.docs_new += 1
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
                deltas.facts_new += 1
                deltas.new_fact_texts.append(fd.text)
                _merge_edge(s, L.FACT, fact_id, E.EXTRACTED_FROM, L.WEB_DOC, url_hash)
                self._link_mentions(s, fact_id, fd.mentions, scope_names,
                                    collected_by, deltas)
        return deltas

    def _link_mentions(self, s, fact_id, mentions, scope_names, collected_by, deltas):
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
                deltas.mentions_confirmed += 1
            else:                                     # 미해소 → Entity
                eid = short_hash(nm)
                _merge_node(s, L.ENTITY, eid, {
                    "name": m.surface, "guess_label": m.guess_label,
                    "collected_by": collected_by})
                deltas.entities_new += 1
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
