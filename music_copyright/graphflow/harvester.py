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
import importlib.util
import json
import logging
import os
import re
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import unquote, urlparse

from neo4j import Driver
from pydantic import BaseModel, Field
from infra.agent.tracing import emit

from music_copyright.graphflow.agents.base import (
    BaseAgent, music_system_prompt, normalize_text, short_hash)
from music_copyright.graphflow.graph.cards import build_card
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.prompts import get_prompt
from music_copyright.graphflow.research import QueryOutcome, ResearchTask
from music_copyright.graphflow.traversal import scope_eids

logger = logging.getLogger("music_copyright.harvester")

_MAX_DOCS_PER_QUERY = 8
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
_DISABLED_SEARCH_PROVIDERS: set[str] = set()


# ───────────────────────────────────────────────────────────
# LLM 추출 스키마
# ───────────────────────────────────────────────────────────
class MentionDraft(BaseModel):
    surface: str = Field(description="문서에 나온 표기 그대로 (곡명·아티스트·차트·수상명 등)")
    guess_label: str | None = Field(default=None,
        description=("추정 종류: Work/Release/Artist/Chart/Award/Rightsholder/Agency/"
                     "Platform/Fandom/Genre/MediaEvent/ViralTrend 또는 null"))


class FactDraft(BaseModel):
    text: str = Field(description="원자적 사실 1문장 (한국어). 숫자·순위·날짜는 문서 표기 그대로")
    source_excerpt: str = Field(
        description="이 사실을 직접 뒷받침하는 문서의 연속된 원문 구절(1~2문장, 그대로 복사)")
    mentions: list[MentionDraft] = Field(default_factory=list)
    event_date: str | None = Field(default=None, description="YYYY-MM(-DD) 또는 null")
    extraction_confidence: float = Field(default=0.6, description="추출 확신도 0~1")


class ExtractionOutput(BaseModel):
    facts: list[FactDraft] = Field(default_factory=list)
    doc_relevance: float = Field(default=0.5, description="문서의 검색맥락 관련도 0~1")


@dataclass
class HarvestStats:
    queries: int = 0
    docs_seen: int = 0
    docs_ingested: int = 0
    docs_new: int = 0
    grounded_facts: int = 0
    facts_new: int = 0
    entities_new: int = 0
    mentions_confirmed: int = 0
    new_fact_texts: list[str] = field(default_factory=list)
    outcomes: list[QueryOutcome] = field(default_factory=list)


@dataclass
class _DocDeltas:
    """문서 1건 적재의 stats 증분 — 스레드 로컬로 모은 뒤 풀 종료 후 직렬 병합.

    각 _ingest_doc 태스크는 공유 HarvestStats를 직접 건드리지 않고 이 값을 반환한다.
    워커는 자기 _DocDeltas만 쓰므로 락이 불필요하고, 병합은 디스패치 스레드가 입력
    순서대로(map) 한 번에 한다(merge_into) — 누적 순서가 직렬 베이스라인과 동일.
    """
    status: str = "skipped"
    reason: str = "unknown"
    source: str = ""
    docs_ingested: int = 0
    docs_new: int = 0
    grounded_facts: int = 0
    facts_new: int = 0
    entities_new: int = 0
    mentions_confirmed: int = 0
    new_fact_texts: list[str] = field(default_factory=list)

    def merge_into(self, stats: HarvestStats) -> None:
        stats.docs_ingested += self.docs_ingested
        stats.docs_new += self.docs_new
        stats.grounded_facts += self.grounded_facts
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

    ctx_tasks = [(contextvars.copy_context(), task) for task in tasks]

    def _run_in_ctx(item: tuple[contextvars.Context, dict]) -> _DocDeltas:
        ctx, task = item
        return ctx.run(ingest_fn, **task)

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="music-harvest") as pool:
        for deltas in pool.map(_run_in_ctx, ctx_tasks):
            results.append(deltas)
    return results


def _round_robin(chunks: list[list[dict]]) -> list[dict]:
    """채널별 결과를 1개씩 교차 병합해 한 채널이 문서 슬롯을 독식하지 않게 한다."""
    out: list[dict] = []
    max_len = max((len(c) for c in chunks), default=0)
    for i in range(max_len):
        for chunk in chunks:
            if i < len(chunk):
                out.append(chunk[i])
    return out


def _rank_docs(docs: list[dict], work_card: str) -> list[dict]:
    """Prefer target-anchored, higher-tier documents while keeping source diversity."""
    title, artist = _work_terms_from_card(work_card)
    title_norm, artist_norm = normalize_text(title), normalize_text(artist)

    def score(d: dict) -> tuple[float, str]:
        body = _doc_context_text(d)
        anchor = 0.0
        if title_norm and title_norm in body:
            anchor += 0.8
        if artist_norm and artist_norm in body:
            anchor += 0.7
        if title_norm and artist_norm and title_norm in body and artist_norm in body:
            anchor += 0.5
        tier = _source_tier(d.get("source"), d.get("channel") or "web")
        return (anchor + tier, d.get("url") or "")

    ranked = sorted(docs, key=lambda d: (-score(d)[0], score(d)[1]))
    selected: list[dict] = []
    by_host: dict[str, int] = {}
    deferred: list[dict] = []
    for d in ranked:
        host = urlparse(d.get("url") or "").netloc.lower() or str(d.get("source") or "")
        if by_host.get(host, 0) >= 2:
            deferred.append(d)
            continue
        selected.append(d)
        by_host[host] = by_host.get(host, 0) + 1
        if len(selected) >= _MAX_DOCS_PER_QUERY:
            return selected
    for d in deferred:
        selected.append(d)
        if len(selected) >= _MAX_DOCS_PER_QUERY:
            break
    return selected


# ───────────────────────────────────────────────────────────
# 스코프 이름 로딩 (mention 링크용)
# ───────────────────────────────────────────────────────────
# 스코프 노드에서 mention 매칭 surface로 읽는 기본 이름 필드.
_SCOPE_NAME_FIELDS = (
    "title", "release_title", "artist_name", "name", "agency_name",
    "genre_name", "chart_name", "award_name", "event_name", "trend_name",
    "fandom_name",
)
# 음차 별칭 리스트 필드 — 라벨별로 자기 노드에만 귀속(원어↔로마자). Work.artist_aliases는
# 카드/grounding용이라 여기선 무시한다(아티스트 mention이 Work로 잘못 가지 않게).
_SCOPE_ALIAS_FIELDS = {L.WORK: ("title_aliases",), L.ARTIST: ("artist_aliases",)}


def _index_scope_names(rows: list[dict]) -> dict[str, tuple[str, str]]:
    """스코프 노드 행 → {정규화 이름/별칭: (label, key)}.

    기본 이름에 더해 음차 별칭(Work.title_aliases·Artist.artist_aliases)도 등록해,
    원어 보도("리센느")의 mention이 영문 노드(RESCENE)에 매칭되게 한다 — 안 하면
    고아 Entity로 중복 생성된다. 순수 함수(테스트 용이)."""
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        label, p = r["label"], r["p"] or {}
        key = p.get(KEY_PROP.get(label, ""))
        if not key:
            continue
        for nk in _SCOPE_NAME_FIELDS:
            v = p.get(nk)
            if v and isinstance(v, str):
                out.setdefault(normalize_text(v), (label, str(key)))
        for ak in _SCOPE_ALIAS_FIELDS.get(label, ()):
            for alias in (p.get(ak) or []):
                if alias and isinstance(alias, str):
                    out.setdefault(normalize_text(alias), (label, str(key)))
    return out


def _load_scope_names(driver: Driver, work_id: str, scope_hops: int) -> dict[str, tuple[str, str]]:
    """Work 스코프 내 노드 이름/별칭 → (label, key). mention surface 매칭에 사용."""
    eids = scope_eids(driver, work_id, scope_hops)
    if not eids:
        return {}
    with driver.session() as s:
        rows = s.run(
            "UNWIND $eids AS eid MATCH (n) WHERE elementId(n)=eid "
            "RETURN labels(n)[0] AS label, properties(n) AS p", eids=list(eids),
        ).data()
    return _index_scope_names(rows)


# "원어(romaja)" / "name(qualifier)" 합성 표면형의 괄호부 제거용.
_PAREN_RE = re.compile(r"\([^)]*\)")


def _match_scope_name(nm: str, scope_names: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """mention 표면형(정규화)을 스코프 노드에 매칭. 정확일치 우선. 부분일치는 과다매칭을
    막는 가드를 둔다 — 짧은 이름(<3)·길이비율<0.6은 substring이라도 매칭하지 않고(예: 스코프
    '위로'가 '위로의노래'에, 'BTS'가 'BTSARMY'에 잘못 걸리는 것 차단), 후보 중 가장 구체적인
    (긴) 이름을 고른다(첫 매치 break의 임의성 제거)."""
    exact = scope_names.get(nm)
    if exact is not None:
        return exact
    if len(nm) < 3:
        return None
    # "원어(romaja)"/"name(qualifier)" 표기는 괄호부를 떼고 정확일치를 재시도한다 — 예
    # "리센느(rescene)" → "리센느" → Artist. 이런 합성 표면형은 부분일치 가드(ratio<0.6)에
    # 막혀 고아 Entity가 되므로, fuzzy가 아니라 정확일치로 안전하게 흡수한다.
    stripped = _PAREN_RE.sub("", nm).strip()
    if stripped and stripped != nm:
        hit = scope_names.get(stripped)
        if hit is not None:
            return hit
    best: tuple[str, str] | None = None
    best_len = 0
    for name, lk in scope_names.items():
        if len(name) < 3 or len(name) <= best_len:
            continue
        short, long = (name, nm) if len(name) <= len(nm) else (nm, name)
        if short in long and len(short) / len(long) >= 0.6:
            best, best_len = lk, len(name)
    return best


def _source_tier(source: str | None, channel: str) -> float:
    if channel == "news":
        return _TIER_NEWS
    if channel == "youtube":
        return 0.64
    s = (source or "").lower()
    if any(good in s for good in (
        "billboard.com", "charts.youtube.com", "spotify.com", "open.spotify",
        "genius.com", "last.fm", "deezer.com", "musicbrainz.org",
    )):
        return 0.82
    if any(b in s for b in (
        "blog", "tistory", "cafe", "namu", "fandom.com", "bulbapedia",
        "bulbagarden", "static.prenatal", "coupert", "vk.com", "yandex",
        "play-games", "azlyrics", "topmusiclyrics",
    )):
        return _TIER_BLOG
    return _TIER_WEB


def _is_gap_nonfact(text: str) -> bool:
    t = normalize_text(text or "")
    return any(p in t for p in (
        "정보는 제공되지 않았습니다",
        "정보가 제공되지 않았습니다",
        "자료에는 제공되지 않았습니다",
        "자료에는 나타나지 않습니다",
        "근거에는 제공되지",
        "언급되지 않았다",
        "언급되지 않았습니다",
        "확인되지 않았다",
        "확인되지 않았습니다",
        "나타나지 않는다",
        "나타나지 않습니다",
        "not provided",
        "no information is provided",
        "no evidence is provided",
    ))


def _work_terms_from_card(work_card: str) -> tuple[str, str]:
    """Work card에서 제목/아티스트를 보수적으로 추출한다."""
    first = (work_card or "").splitlines()[0]
    if " — " not in first:
        return "", ""
    title, rest = first.split(" — ", 1)
    artist = rest.split("는", 1)[0].strip()
    return title.strip(), artist


_CARD_ALIAS_RE = re.compile(r"\[별칭\]\s*제목:\s*([^·]*)·\s*아티스트:\s*(.*)$")


def _work_aliases_from_card(work_card: str) -> tuple[list[str], list[str]]:
    """카드의 "[별칭] 제목: a|b · 아티스트: x|y" 라인 파싱. 없으면 ([], [])."""
    m = _CARD_ALIAS_RE.search(work_card or "")
    if not m:
        return [], []

    def _split(s: str) -> list[str]:
        return [x.strip() for x in s.split("|") if x.strip() and x.strip() != "-"]

    return _split(m.group(1)), _split(m.group(2))


def _work_alias_norms(work_card: str) -> tuple[set[str], set[str]]:
    """(제목 별칭 정규화 집합, 아티스트 별칭 정규화 집합) — 1차 제목/아티스트 + 카드 별칭.

    음차 불일치(영문 카드 ↔ 원어 보도)를 메운다 — doc/excerpt가 어느 표기든 들고 있으면 동일
    작품으로 인정. normalize_text는 공백 제거+소문자라 표기 변형에 견고하다."""
    title, artist = _work_terms_from_card(work_card)
    t_al, a_al = _work_aliases_from_card(work_card)
    titles = {normalize_text(x) for x in (title, *t_al) if x and x.strip()}
    artists = {normalize_text(x) for x in (artist, *a_al) if x and x.strip()}
    return {t for t in titles if t}, {a for a in artists if a}


def _any_alias_in(aliases: set[str], text_norm: str) -> bool:
    return any(al in text_norm for al in aliases if al)


def _work_identifier_from_card(work_card: str) -> str:
    m = re.search(r"작품식별자\s+([^\s.]+)", work_card or "")
    return m.group(1) if m else ""


def _doc_context_text(d: dict) -> str:
    raw = " ".join(str(d.get(k) or "") for k in (
        "title", "content", "source", "url", "youtube_channel",
    ))
    return normalize_text(unquote(raw))


def _missing_required_artist_context(d: dict, query: str, work_card: str) -> bool:
    """짧은 일반명사 곡명의 검색 오염을 LLM 추출 전에 차단한다.

    예: "토끼 공익 캠페인"은 동물·교육·해외 기사까지 끌어온다. 이런 경우 문서 자체에
    performer가 없으면 검색어 맥락만 보고 LLM이 평가 대상 Fact를 지어내기 쉽다.
    """
    title, artist = _work_terms_from_card(work_card)
    title_norms, artist_norms = _work_alias_norms(work_card)
    title_norm = normalize_text(title)
    artist_norm = normalize_text(artist)
    query_norm = normalize_text(query)
    if not title_norm or not artist_norm or len(title_norm) > 4:
        # artist-only 검색도 문서에 아티스트 표기(별칭 포함)가 없으면 수집하지 않는다. 평가
        # 대상 카드가 프롬프트에 들어가므로, 원문 앵커가 없으면 LLM이 대상 정보를 투사하기 쉽다.
        return artist_norm in query_norm and not _any_alias_in(artist_norms, _doc_context_text(d))
    combined = _doc_context_text(d)
    if artist_norm in query_norm and not _any_alias_in(artist_norms, combined):
        return True
    if title_norm in query_norm:
        return not _any_alias_in(title_norms, combined) or not _any_alias_in(artist_norms, combined)
    return False


# 곡을 가리키는 대용/축약 표현(정규화형 — normalize_text가 공백 제거+소문자화). excerpt가
# 제목을 직접 적지 않아도 이 표현이 있으면 동일 곡을 지칭하는 정상 grounding으로 본다.
_WORK_ANAPHORA: tuple[str, ...] = (
    "이곡", "그곡", "해당곡", "동곡", "위곡", "본곡", "이노래", "그노래", "해당노래",
    "이작품", "해당작품", "이싱글", "이트랙", "타이틀곡", "이음원", "이음악",
    "thesong", "thissong", "thetrack", "thistrack", "thesingle",
)

# 보고 관행상 곡명은 따옴표로 인용된다(예: 'Dynamite', '다이너마이트'). 직선·곡선 모두.
_QUOTED_TITLE_RE = re.compile(r"['\"‘’“”]([^'\"‘’“”\n]{2,40})['\"‘’“”]")


def _asserted_work_titles(fact_text: str, fact_norm: str, title_norm: str) -> set[str]:
    """Fact가 곡(평가 대상)을 주어로 단정할 때 쓰는 곡명 표면형(정규화) 집합.

    이 표면형들은 카드(프롬프트)가 아니라 근거 구절이 실제로 지칭해야 하는 대상이다.
    따옴표로 인용된 곡명과, 카드 제목이 Fact에 그대로 나타난 경우를 모은다. 카드 제목이
    영문('Dynamite')이고 Fact가 음차('다이너마이트')를 써도 인용 곡명으로 포착된다.
    """
    titles = {normalize_text(q) for q in _QUOTED_TITLE_RE.findall(fact_text or "")}
    if title_norm and title_norm in fact_norm:
        titles.add(title_norm)
    return {t for t in titles if len(t) >= 2}


def _fact_grounded_in_doc(fd: FactDraft, d: dict, query: str, work_card: str) -> bool:
    """추출 Fact가 평가 대상 카드가 아니라 실제 문서 스니펫에 근거하는지 확인한다."""
    title, artist = _work_terms_from_card(work_card)
    title_norms, artist_norms = _work_alias_norms(work_card)
    identifier = _work_identifier_from_card(work_card)
    doc_norm = _doc_context_text(d)
    fact_norm = normalize_text(fd.text)
    title_norm = normalize_text(title)
    artist_norm = normalize_text(artist)
    ident_norm = normalize_text(identifier)
    query_norm = normalize_text(query)
    excerpt_norm = normalize_text(fd.source_excerpt or "")

    # 엔티티 일치만으로는 함의를 보장하지 못한다. 추출기가 제시한 원문 구절이 실제
    # 문서에 연속해서 존재하고, Fact의 핵심 어휘·수치가 그 구절에 있어야 한다.
    if len(excerpt_norm) < 8 or excerpt_norm not in doc_norm:
        return False
    numbers = re.findall(r"\d[\d,.%]*", fd.text or "")
    if any(number not in (fd.source_excerpt or "") for number in numbers):
        return False
    fact_tokens = set(re.findall(
        r"[0-9A-Za-z가-힣]{2,}", (fd.text or "").lower()))
    excerpt_tokens = set(re.findall(
        r"[0-9A-Za-z가-힣]{2,}", (fd.source_excerpt or "").lower()))
    anchor_tokens = {
        t for value in (title_norm, artist_norm)
        for t in re.findall(r"[0-9A-Za-z가-힣]{2,}", value.lower())
    }
    meaningful = fact_tokens - anchor_tokens - {
        "이라고", "라는", "대해", "통해", "위해", "있다", "했다", "한다",
        "해당", "작품", "음악", "노래", "곡은", "곡을",
    }
    if len(meaningful) >= 3:
        supported = len(meaningful & excerpt_tokens) / len(meaningful)
        if supported < 0.4:
            return False

    # 평가 대상(곡)을 주어로 단정하는 Fact는 그 곡이 source_excerpt에도 실제로
    # 지칭되어야 한다 — 곡명 표면형이 직접 있거나, 곡을 가리키는 대용 표현(이 곡 등)이
    # 있을 때만 통과. 둘 다 없으면 excerpt가 말하는 제3의 주체(멤버·타인)의 행위를 곡으로
    # 과대귀속한 것이다(예: RM의 법안 지지를 "'다이너마이트'는 …와 관련"으로 둔갑). 핵심
    # 어휘·수치(위 검증)는 excerpt의 술어부와 겹쳐도 통과하므로, 주어 앵커를 별도로 본다.
    # excerpt가 곡명 표면형·곡 별칭(원어/음차 표기)·대용표현 중 하나라도 지칭해야 곡 주어
    # 단정의 근거다. 곡 별칭(예: 'Dynamite'↔'다이너마이트')은 음차 차이를 메운다. 단 아티스트
    # 표기는 여기에 넣지 않는다 — excerpt가 곡이 아니라 아티스트·멤버의 별도 행위를 말하면
    # 곡 주어로 과대귀속되기 때문(아티스트 별칭은 doc-레벨 게이트에서만 쓴다).
    asserted_titles = _asserted_work_titles(fd.text or "", fact_norm, title_norm)
    if (asserted_titles
            and not any(t in excerpt_norm for t in asserted_titles)
            and not _any_alias_in(title_norms, excerpt_norm)
            and not any(a in excerpt_norm for a in _WORK_ANAPHORA)):
        return False

    # 제목/아티스트가 Fact에 단정됐는데 doc에 어떤 표기(별칭)도 없으면 과대귀속 — 단, 별칭 중
    # 하나라도 doc에 있으면 음차 차이여도 통과.
    if ident_norm and ident_norm in fact_norm and ident_norm not in doc_norm:
        return False
    if title_norm and title_norm in fact_norm and not _any_alias_in(title_norms, doc_norm):
        return False
    if artist_norm and artist_norm in fact_norm and not _any_alias_in(artist_norms, doc_norm):
        return False
    # 짧은 일반명사 곡명은 제목만 맞아도 오염될 수 있어, 작품 검색에서 두 앵커(별칭)를 요구한다.
    if (title_norm and artist_norm and len(title_norm) <= 4 and
            title_norm in query_norm and title_norm in fact_norm):
        return _any_alias_in(title_norms, doc_norm) and _any_alias_in(artist_norms, doc_norm)
    return True


def _doc_mentions_target(d: dict, work_card: str) -> bool:
    title_norms, artist_norms = _work_alias_norms(work_card)
    if not title_norms or not artist_norms:
        return False
    doc_norm = _doc_context_text(d)
    shortest_title = min((len(t) for t in title_norms), default=99)
    if shortest_title <= 4:   # 짧은 일반명사 곡명은 두 앵커(별칭) 모두 요구
        return _any_alias_in(title_norms, doc_norm) and _any_alias_in(artist_norms, doc_norm)
    return _any_alias_in(title_norms, doc_norm) or _any_alias_in(artist_norms, doc_norm)


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
        if tav_key and "tavily" not in _DISABLED_SEARCH_PROVIDERS:
            from infra.tool.web import TavilyWebSearchTool
            self._tavily = TavilyWebSearchTool(tav_key)
        self._naver = None
        if os.environ.get("NAVER_CLIENT_ID"):
            from infra.tool.naver import NaverNewsSearchTool
            self._naver = NaverNewsSearchTool()
        self._youtube = None
        if os.environ.get("YOUTUBE_SEARCH_ENABLED", "1") != "0":
            if importlib.util.find_spec("yt_dlp") is not None:
                from infra.tool.youtube import YouTubeSearchTool
                self._youtube = YouTubeSearchTool()
            else:
                logger.info("youtube search skip: yt-dlp not installed")

    # ── 검색 (채널 통합) ─────────────────────────────────────
    def _search(self, query: str, *, channels: list[str] | None = None,
                work_card: str = "", task: ResearchTask | None = None,
                strategy: str = "discovery") -> list[dict]:
        wanted = set(channels or ("web", "news", "youtube"))
        tasks = []
        if "web" in wanted:
            tasks.append(("ddgs_text", lambda: self._ddgs_docs(query, "text", "web")))
        if "news" in wanted:
            tasks.append(("ddgs_news", lambda: self._ddgs_docs(query, "news", "news")))
        if self._youtube and "youtube" in wanted:
            tasks.append(("youtube", lambda: self._youtube_docs(query)))
        if self._tavily and "news" in wanted:
            tasks.append(("tavily", lambda: self._tavily_docs(query)))
        if self._naver and "news" in wanted:
            tasks.append(("naver", lambda: self._naver_docs(query)))

        def _run(task):
            name, fn = task
            try:
                return name, fn()
            except Exception as exc:  # noqa: BLE001
                message = str(exc).lower()
                if name == "tavily" and (
                        "http 432" in message or "usage limit" in message or
                        "exceeds your plan" in message):
                    first = name not in _DISABLED_SEARCH_PROVIDERS
                    _DISABLED_SEARCH_PROVIDERS.add(name)
                    self._tavily = None
                    if first:
                        emit("search_provider_disabled", provider=name,
                             reason="quota_exhausted")
                logger.debug("%s skip: %s", name, exc)
                return name, []

        if not tasks:
            named_chunks = []
        elif len(tasks) == 1:
            named_chunks = [_run(tasks[0])]
        else:
            ctx_tasks = [(contextvars.copy_context(), task) for task in tasks]

            def _run_in_ctx(item):
                ctx, task = item
                return ctx.run(_run, task)

            with ThreadPoolExecutor(max_workers=min(len(tasks), 5),
                                    thread_name_prefix="music-search") as pool:
                named_chunks = list(pool.map(_run_in_ctx, ctx_tasks))
        chunks = [chunk for _, chunk in named_chunks]
        docs = _round_robin(chunks)
        # url 중복 제거 + content 있는 것만
        seen, uniq = set(), []
        for d in docs:
            if d["url"] and d["url"] not in seen and (d["content"] or "").strip():
                seen.add(d["url"])
                uniq.append(d)
        selected = _rank_docs(uniq, work_card)[:_MAX_DOCS_PER_QUERY]
        emit(
            "harvest_search",
            query=query,
            task_id=task.task_id if task else "",
            target=task.target if task else "",
            axis=task.axis if task else "",
            strategy=strategy,
            planned_channels=sorted(wanted),
            channels={name: len(chunk) for name, chunk in named_chunks},
            selected=len(selected),
            sources=[d.get("source") for d in selected[:8]],
        )
        return selected

    def _ddgs_docs(self, query: str, topic: str, channel: str) -> list[dict]:
        res = self._ddgs(query=query, max_results=5, topic=topic)
        return [self._doc(it, channel) for it in res.get("items", [])]

    def _tavily_docs(self, query: str) -> list[dict]:
        res = self._tavily(query=query, max_results=5, topic="news")
        return [self._doc(it, "news") for it in res.get("items", [])]

    def _naver_docs(self, query: str) -> list[dict]:
        res = self._naver(query=query, display=8, sort="sim")
        return [
            self._doc(
                {"title": it.get("title"), "link": it.get("link"),
                 "content": it.get("description"), "pub_date": it.get("pub_date"),
                 "source": "naver"}, "news")
            for it in res.get("items", [])
        ]

    def _youtube_docs(self, query: str) -> list[dict]:
        res = self._youtube(query=query, max_results=3)
        return [self._doc(it, "youtube") for it in res.get("items", [])]

    @staticmethod
    def _doc(it: dict, channel: str) -> dict:
        return {
            "title": it.get("title") or "", "url": it.get("link") or "",
            "content": it.get("content") or "", "source": it.get("source"),
            "published_at": it.get("pub_date") or "", "channel": channel,
            "video_id": it.get("video_id"),
            "youtube_channel": it.get("channel"),
            "view_count": it.get("view_count"),
            "like_count": it.get("like_count"),
        }

    # ── 메인 ─────────────────────────────────────────────────
    def harvest(
        self,
        driver: Driver,
        *,
        work_id: str,
        work_card: str,
        plans: list[ResearchTask],
        query_hashes: dict[str, str],
        target_cards: dict[str, str],
        collected_by: str,
        scope_hops: int = 3,
    ) -> HarvestStats:
        stats = HarvestStats()
        scope_names = _load_scope_names(driver, work_id, scope_hops)
        for plan in plans:
            t_label, _, t_key = plan.target.partition(":")
            for variant in plan.query_variants():
                query = variant.text
                stats.queries += 1
                docs = self._search(
                    query, channels=variant.channels, work_card=work_card,
                    task=plan, strategy=variant.strategy)
                stats.docs_seen += len(docs)
                qhash = query_hashes.get(query) or short_hash(
                    f"{work_id}|{plan.target}|{normalize_text(query)}")
                # 모은 문서들의 LLM 추출·적재만 스레드풀로 병렬(_parallel_ingest).
                tasks = [dict(driver=driver, d=d, query=query, qhash=qhash,
                              t_label=t_label, t_key=t_key, work_card=work_card,
                              work_id=work_id, scope_names=scope_names,
                              collected_by=collected_by)
                         for d in docs]
                outcome = QueryOutcome(
                    task_id=plan.task_id,
                    target=plan.target,
                    axis=plan.axis,
                    query=query,
                    strategy=variant.strategy,
                    channels=list(variant.channels),
                    docs_selected=len(docs),
                )
                for deltas in _parallel_ingest(self._ingest_doc, tasks):
                    deltas.merge_into(stats)
                    outcome.docs_ingested += deltas.docs_ingested
                    outcome.docs_new += deltas.docs_new
                    outcome.grounded_facts += deltas.grounded_facts
                    outcome.facts_new += deltas.facts_new
                    outcome.entities_new += deltas.entities_new
                    outcome.mentions_confirmed += deltas.mentions_confirmed
                    if deltas.status == "ingested" and deltas.source:
                        outcome.sources.add(deltas.source)
                    if deltas.status != "ingested":
                        outcome.skip_reasons[deltas.reason] = (
                            outcome.skip_reasons.get(deltas.reason, 0) + 1)
                stats.outcomes.append(outcome)
                self._persist_query_outcome(driver, qhash, outcome)
                emit("harvest_query_outcome", **outcome.as_dict())
        logger.info("music_harvest: q=%d docs=%d/%d new=%d facts=%d/%d ent=%d ment=%d",
                    stats.queries, stats.docs_ingested, stats.docs_seen, stats.docs_new,
                    stats.grounded_facts, stats.facts_new,
                    stats.entities_new, stats.mentions_confirmed)
        return stats

    @staticmethod
    def _persist_query_outcome(driver: Driver, qhash: str, outcome: QueryOutcome) -> None:
        data = outcome.as_dict()
        with driver.session() as s:
            s.run(
                f"""
                MATCH (q:{L.QUERY} {{{KEY_PROP[L.QUERY]}: $h}})
                SET q.docs_selected = $docs_selected,
                    q.docs_ingested = $docs_ingested,
                    q.docs_new = $docs_new,
                    q.grounded_facts = $grounded_facts,
                    q.facts_new = $facts_new,
                    q.entities_new = $entities_new,
                    q.mentions_confirmed = $mentions_confirmed,
                    q.utility = $utility,
                    q.outcome_sources = $sources,
                    q.skip_reasons_json = $skip_reasons,
                    q.outcome_recorded_at = datetime()
                """,
                h=qhash,
                docs_selected=data["docs_selected"],
                docs_ingested=data["docs_ingested"],
                docs_new=data["docs_new"],
                grounded_facts=data["grounded_facts"],
                facts_new=data["facts_new"],
                entities_new=data["entities_new"],
                mentions_confirmed=data["mentions_confirmed"],
                utility=data["utility"],
                sources=data["sources"],
                skip_reasons=json.dumps(data["skip_reasons"], ensure_ascii=False),
            ).consume()

    # stats를 직접 갱신하지 않고 _DocDeltas를 반환한다 — 쿼리 내 문서들을 스레드풀로
    # 동시 추출·적재하기 위함(_parallel_ingest). 워커는 자기 driver.session()으로만
    # 적재하므로 공유 가변상태가 없고, 증분 병합은 디스패치 스레드가 직렬로 한다.
    def _ingest_doc(self, driver, d, query, qhash, t_label, t_key, work_card,
                    work_id, scope_names, collected_by) -> "_DocDeltas":
        deltas = _DocDeltas(source=str(d.get("source") or d.get("channel") or ""))
        if _missing_required_artist_context(d, query, work_card):
            deltas.reason = "missing_artist_context"
            emit("harvest_doc", status="skipped", reason="missing_artist_context",
                 query=query, url=d.get("url"), source=d.get("source"),
                 channel=d.get("channel"))
            return deltas
        # 1. Fact 추출 (LLM)
        try:
            out: ExtractionOutput = self.complete(
                system=music_system_prompt(get_prompt("music_harvester.system")),
                user=(f"[평가 대상]\n{work_card}\n\n[검색어]\n{query}\n\n"
                      f"[문서] {d['title']}\nURL: {d['url']}\n{d['content'][:_BODY_TRIM]}"),
                schema=ExtractionOutput)
        except Exception as exc:  # noqa: BLE001
            deltas.reason = "extract_error"
            emit("harvest_doc", status="extract_error", query=query, url=d.get("url"),
                 source=d.get("source"), channel=d.get("channel"),
                 error=type(exc).__name__)
            logger.debug("extract skip: %s", exc)
            return deltas
        if out.doc_relevance < _MIN_DOC_RELEVANCE or not out.facts:
            deltas.reason = "low_relevance_or_empty"
            emit("harvest_doc", status="skipped", reason="low_relevance_or_empty",
                 query=query, url=d.get("url"), source=d.get("source"),
                 channel=d.get("channel"), relevance=round(float(out.doc_relevance), 3),
                 facts=len(out.facts))
            return deltas
        grounded_facts = [
            fd for fd in out.facts
            if _fact_grounded_in_doc(fd, d, query, work_card)
        ]
        if not grounded_facts:
            deltas.reason = "ungrounded_facts"
            emit("harvest_doc", status="skipped", reason="ungrounded_facts",
                 query=query, url=d.get("url"), source=d.get("source"),
                 channel=d.get("channel"), relevance=round(float(out.doc_relevance), 3),
                 facts=len(out.facts))
            return deltas

        tier = _source_tier(d["source"], d["channel"])
        url_hash = short_hash(d["url"])
        with driver.session() as s:
            # WebDoc + RETRIEVED + ABOUT(target)
            doc_created = _merge_node(s, L.WEB_DOC, url_hash, {
                "title": d["title"], "url": d["url"], "source": d["source"],
                "published_at": d["published_at"], "source_tier": tier,
                "channel": d.get("channel"), "video_id": d.get("video_id"),
                "youtube_channel": d.get("youtube_channel"),
                "view_count": d.get("view_count"), "like_count": d.get("like_count"),
                "collected_by": collected_by})
            deltas.status = "ingested"
            deltas.reason = ""
            deltas.docs_ingested = 1
            deltas.docs_new += int(doc_created)
            _merge_edge(s, L.QUERY, qhash, E.RETRIEVED, L.WEB_DOC, url_hash)
            if t_key:
                _merge_edge(s, L.WEB_DOC, url_hash, E.ABOUT, t_label, t_key)

            self._ingest_youtube_metrics(s, driver, d, url_hash, qhash, work_card,
                                         collected_by, tier, deltas)

            for fd in grounded_facts[:_MAX_FACTS_PER_DOC]:
                if not fd.text.strip():
                    continue
                if _is_gap_nonfact(fd.text):
                    continue
                fact_id = short_hash(f"{work_id}|{normalize_text(fd.text)}")
                conf = round(min(1.0, tier * float(fd.extraction_confidence or 0.6)), 3)
                fact_created = _merge_node(s, L.FACT, fact_id, {
                    "text": fd.text, "published_at": d["published_at"] or None,
                    "event_date": fd.event_date, "confidence": conf,
                    "source_tier": tier, "source_excerpt": fd.source_excerpt[:500],
                    "extraction_method": "llm_with_excerpt",
                    "collected_by": collected_by})
                deltas.grounded_facts += 1
                deltas.facts_new += int(fact_created)
                if fact_created:
                    deltas.new_fact_texts.append(fd.text)
                _merge_edge(s, L.FACT, fact_id, E.EXTRACTED_FROM, L.WEB_DOC, url_hash)
                _merge_edge(s, L.FACT, fact_id, E.EXTRACTED_BY, L.QUERY, qhash)
                self._link_mentions(s, fact_id, fd.mentions, scope_names,
                                    work_id, collected_by, deltas)
        emit("harvest_doc", status="ingested", query=query, url=d.get("url"),
             source=d.get("source"), channel=d.get("channel"),
             relevance=round(float(out.doc_relevance), 3), facts=len(grounded_facts),
             mentions=deltas.mentions_confirmed, entities=deltas.entities_new)
        return deltas

    def _ingest_youtube_metrics(self, s, driver, d, url_hash, qhash, work_card,
                                collected_by, tier, deltas) -> None:
        """YouTube public metadata를 일반 WebDoc에 묻지 않고 metric/fact로도 적재."""
        if d.get("channel") != "youtube" or not d.get("video_id"):
            return
        view_count = d.get("view_count")
        like_count = d.get("like_count")
        if view_count is None and like_count is None:
            return
        work_key = _work_key_from_card(driver, work_card)
        if not work_key:
            return
        if not _doc_mentions_target(d, work_card):
            return
        observed_at = datetime.now().date().isoformat()
        platform = _merge_node(s, L.PLATFORM, f"youtube:{work_key}", {
            "name": "YouTube",
        })
        _merge_edge(s, L.WORK, work_key, E.DISTRIBUTED_ON, L.PLATFORM, f"youtube:{work_key}")
        if view_count is not None:
            metric_id = short_hash(f"{work_key}:youtube:{d['video_id']}:views:{observed_at}")
            _merge_node(s, L.SOCIAL_METRIC, metric_id, {
                "platform": "YouTube", "metric": "views", "value": view_count,
                "as_of": observed_at, "source": "youtube.com", "url": d.get("url"),
                "video_title": d.get("title"), "channel": d.get("youtube_channel"),
                "video_id": d.get("video_id"), "confidence": tier,
                "source_tier": tier, "collected_by": collected_by,
            })
            _merge_edge(s, L.WORK, work_key, E.HAS_METRIC, L.SOCIAL_METRIC, metric_id,
                        {"value": view_count})
            _merge_edge(s, L.PLATFORM, f"youtube:{work_key}", E.HAS_METRIC,
                        L.SOCIAL_METRIC, metric_id, {"value": view_count})
            fact_text = (
                f"YouTube 영상 '{d.get('title')}'"
                + (f"(채널 {d.get('youtube_channel')})" if d.get("youtube_channel") else "")
                + f"은 조회수 {int(view_count):,}회를 기록했다."
            )
            fact_id = short_hash(normalize_text(fact_text))
            fact_created = _merge_node(s, L.FACT, fact_id, {
                "text": fact_text, "published_at": d.get("published_at") or None,
                "event_date": d.get("published_at") or None, "confidence": tier,
                "source_tier": tier, "collected_by": collected_by,
                "source_excerpt": (
                    f"video_title={d.get('title')}; view_count={int(view_count)}"),
                "extraction_method": "youtube_metadata",
            })
            deltas.grounded_facts += 1
            deltas.facts_new += int(fact_created)
            if fact_created:
                deltas.new_fact_texts.append(fact_text)
            _merge_edge(s, L.FACT, fact_id, E.EXTRACTED_FROM, L.WEB_DOC, url_hash)
            _merge_edge(s, L.FACT, fact_id, E.EXTRACTED_BY, L.QUERY, qhash)
            _merge_edge(s, L.FACT, fact_id, E.MENTIONS, L.WORK, work_key)
            _merge_edge(s, L.FACT, fact_id, E.MENTIONS, L.PLATFORM, f"youtube:{work_key}")
            _merge_edge(s, L.FACT, fact_id, E.MENTIONS, L.SOCIAL_METRIC, metric_id)
            deltas.mentions_confirmed += 3

    def _link_mentions(self, s, fact_id, mentions, scope_names, work_id,
                       collected_by, deltas):
        for m in mentions:
            nm = normalize_text(m.surface)
            if not nm:
                continue
            matched = _match_scope_name(nm, scope_names)   # 정확일치 + 가드된 부분일치
            if matched:
                label, key = matched
                _merge_edge(s, L.FACT, fact_id, E.MENTIONS, label, key)
                deltas.mentions_confirmed += 1
            else:                                     # 미해소 → Entity
                eid = short_hash(f"{work_id}|{nm}")
                entity_created = _merge_node(s, L.ENTITY, eid, {
                    "name": m.surface, "guess_label": m.guess_label,
                    "collected_by": collected_by})
                deltas.entities_new += int(entity_created)
                _merge_edge(s, L.FACT, fact_id, E.MENTIONS, L.ENTITY, eid)


# ───────────────────────────────────────────────────────────
# 적재 헬퍼 (단건 MERGE)
# ───────────────────────────────────────────────────────────
def _merge_node(session, label: str, key: str, props: dict) -> bool:
    """MERGE a node and report whether this call created it.

    The marker exists only inside the query. Fact confidence/source tier keep
    their strongest observed value when corroborating sources merge.
    """
    key_prop = KEY_PROP[label]
    clean = {k: v for k, v in props.items() if v is not None and v != ""}
    clean[key_prop] = key
    card = build_card(label, clean)
    marker = f"{time.time_ns()}:{id(clean)}"
    prop_map = {k: v for k, v in clean.items() if k != key_prop}
    if label == L.FACT:
        row = session.run(
            f"""
            MERGE (n:{label} {{{key_prop}: $key}})
            ON CREATE SET n.__harvest_created = $marker
            WITH n, n.__harvest_created = $marker AS created,
                 n.confidence AS prior_confidence, n.source_tier AS prior_tier
            SET n += $props, n.card_text = $card
            SET n.confidence = CASE
                    WHEN prior_confidence IS NULL THEN $confidence
                    WHEN $confidence IS NULL THEN prior_confidence
                    WHEN prior_confidence >= $confidence THEN prior_confidence
                    ELSE $confidence END,
                n.source_tier = CASE
                    WHEN prior_tier IS NULL THEN $source_tier
                    WHEN $source_tier IS NULL THEN prior_tier
                    WHEN prior_tier >= $source_tier THEN prior_tier
                    ELSE $source_tier END
            REMOVE n.__harvest_created
            RETURN created
            """,
            key=key, props=prop_map, card=card, marker=marker,
            confidence=prop_map.get("confidence"), source_tier=prop_map.get("source_tier"),
        ).single()
    else:
        row = session.run(
            f"""
            MERGE (n:{label} {{{key_prop}: $key}})
            ON CREATE SET n.__harvest_created = $marker
            WITH n, n.__harvest_created = $marker AS created
            SET n += $props, n.card_text = $card
            REMOVE n.__harvest_created
            RETURN created
            """,
            key=key, props=prop_map, card=card, marker=marker,
        ).single()
    return bool(row and row["created"])


def _work_key_from_card(driver: Driver, work_card: str) -> str | None:
    # 호출부는 이미 work_id를 갖고 있지만, 기존 시그니처를 크게 흔들지 않기 위한
    # 작은 fallback이다. card_text 끝의 "작품식별자 ..." 형식을 우선 사용한다.
    import re
    m = re.search(r"작품식별자\s+([^\s]+)", work_card or "")
    if m:
        return m.group(1).rstrip(".")
    with driver.session() as s:
        r = s.run("MATCH (w:Work {card_text: $card}) RETURN w.work_id AS id LIMIT 1",
                  card=work_card).single()
    return r["id"] if r else None


def _merge_edge(session, src_label, src_key, rel, dst_label, dst_key, props=None) -> None:
    sk, dk = KEY_PROP[src_label], KEY_PROP[dst_label]
    session.run(
        f"MATCH (s:{src_label} {{{sk}: $src}}) MATCH (d:{dst_label} {{{dk}: $dst}}) "
        f"MERGE (s)-[r:{rel}]->(d) SET r += $props",
        src=src_key, dst=dst_key, props=props or {})
