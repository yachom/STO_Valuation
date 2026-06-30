"""Seeder (음악저작권) — WorkIdentity 1건 → 자산 스코프 그래프 적재.

real_estate/graphflow/seeder.py의 2단 구조를 미러링한다:
    1. collect_scope(identity, ...) -> ScopeGraph   # 구조화 API, Neo4j 없이 오프라인 검증
    2. seed(scope, driver, embed=...)  -> dict        # Neo4j MERGE 적재 (+ 선택 임베딩)

real_estate는 SQLite에서 구조 데이터를 긁지만, 음악은 공개 API(MusicBrainz 관계·
Deezer·Last.fm)에서 긁는다 — web-search-first의 '구조' 부분. 웹검색 기반 차트·수상·
영향력 보강은 EXPAND(navigator+harvester)의 몫이다(여기서 안 한다).

결정적·idempotent. 같은 작품 재평가 시 MERGE로 갱신만 일어난다.

이번 스코프:
    Work(앵커), Artist(연주/가창 + 작곡·작사), Platform(Spotify/Deezer/Last.fm).
deferred (EXPAND): Chart(차트성적)·Award(수상)·Rightsholder(권리자)·WebDoc/Fact.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

from neo4j import Driver

from music_copyright.graphflow.graph.cards import build_card
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.external.deezer import deezer_track_by_isrc
from music_copyright.external.genius import genius_song
from music_copyright.external.lastfm import lastfm_track_info
from music_copyright.external.musicbrainz import MusicBrainzClient
from music_copyright.external.soundcharts import soundcharts_metrics
from music_copyright.external.wikidata import wikidata_enrich
from music_copyright.resolver import WorkIdentity

logger = logging.getLogger("music_copyright.seeder")


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _short(s: str, n: int = 16) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


# ───────────────────────────────────────────────────────────
# 스코프 그래프 (직렬화 가능한 중간 표현) — real_estate와 동형
# ───────────────────────────────────────────────────────────
@dataclass
class SeedNode:
    label: str
    key: str
    props: dict = field(default_factory=dict)
    card_text: str = ""


@dataclass
class SeedEdge:
    src_label: str
    src_key: str
    rel: str
    dst_label: str
    dst_key: str
    props: dict = field(default_factory=dict)


@dataclass
class ScopeGraph:
    nodes: list[SeedNode] = field(default_factory=list)
    edges: list[SeedEdge] = field(default_factory=list)
    _index: dict = field(default_factory=dict, repr=False)  # (label,key)->SeedNode

    def add_node(self, label: str, key, props: dict) -> SeedNode | None:
        """노드 추가 (dedup). key None/빈값이면 skip. 같은 (label,key)는 props 병합."""
        if key is None or key == "":
            return None
        key = str(key)
        clean = {k: v for k, v in props.items() if v is not None and v != ""}
        clean[KEY_PROP[label]] = key
        existing = self._index.get((label, key))
        if existing is not None:
            existing.props.update(clean)
            existing.card_text = build_card(label, existing.props)
            return existing
        node = SeedNode(label=label, key=key, props=clean,
                        card_text=build_card(label, clean))
        self.nodes.append(node)
        self._index[(label, key)] = node
        return node

    def add_edge(self, src: SeedNode | None, rel: str, dst: SeedNode | None,
                 props: dict | None = None) -> None:
        if src is None or dst is None:
            return
        self.edges.append(SeedEdge(
            src_label=src.label, src_key=src.key, rel=rel,
            dst_label=dst.label, dst_key=dst.key,
            props={k: v for k, v in (props or {}).items() if v is not None},
        ))

    def summary(self) -> dict:
        by_label: dict[str, int] = {}
        for n in self.nodes:
            by_label[n.label] = by_label.get(n.label, 0) + 1
        by_rel: dict[str, int] = {}
        for e in self.edges:
            by_rel[e.rel] = by_rel.get(e.rel, 0) + 1
        return {"nodes": len(self.nodes), "edges": len(self.edges),
                "by_label": by_label, "by_rel": by_rel}


# ───────────────────────────────────────────────────────────
# 1단계: 스코프 수집 (구조화 API — Neo4j 없이 검증 가능)
# ───────────────────────────────────────────────────────────
def collect_scope(
    identity: WorkIdentity,
    *,
    lastfm_api_key: str | None = None,
    mb_user_agent: str | None = None,
    genius_token: str | None = None,
    soundcharts_app_id: str | None = None,
    soundcharts_api_key: str | None = None,
) -> ScopeGraph:
    """WorkIdentity → 자산 스코프 ScopeGraph.

    각 소스 키가 None이면 해당 보강을 건너뛴다(graceful). 수집물:
        Work(앵커) + 연주/창작 Artist + 유통 Platform(Spotify/Deezer/Last.fm)
        + 정형 보강 Award·Rightsholder·Chart·SocialMetric·커버/샘플
        (Wikidata·MusicBrainz·Genius·Soundcharts — 웹검색이 못 주는 정형 신호).
    """
    sg = ScopeGraph()

    work = sg.add_node(L.WORK, identity.work_id, {
        "title": identity.title,
        "artist_name": identity.artist,
        # 음차 별칭(다국어 표기) — grounding이 영문/원어 어느 표기든 같은 작품으로 인정.
        "title_aliases": identity.title_aliases or None,
        "artist_aliases": identity.artist_aliases or None,
        "release_year": identity.release_year,
        "iswc": identity.iswc,
        "isrc": identity.isrc,
        "spotify_track_id": identity.spotify_track_id,
        "album": identity.album,
    })

    _collect_artists(sg, work, identity, mb_user_agent)
    _collect_platforms(sg, work, identity, lastfm_api_key)
    _collect_structured(sg, work, identity, mb_user_agent=mb_user_agent,
                        genius_token=genius_token,
                        soundcharts_app_id=soundcharts_app_id,
                        soundcharts_api_key=soundcharts_api_key)

    logger.info("collect_scope %s → %s", identity.work_id, sg.summary())
    return sg


def _slug(text: str | None) -> str | None:
    if not text:
        return None
    return "name:" + re.sub(r"\s+", "_", text.strip().lower())


def _collect_artists(sg, work, identity: WorkIdentity, mb_user_agent) -> None:
    """연주/가창 아티스트(Spotify) + 작곡·작사 창작자(MusicBrainz work-rels)."""
    # 연주/가창 — identity에서 (Spotify 아티스트 ID 우선, 없으면 이름 슬러그)
    perf_key = (identity.spotify_artist_ids or [None])[0] or _slug(identity.artist)
    if perf_key:
        performer = sg.add_node(L.ARTIST, perf_key, {
            "name": identity.artist, "role": "performer",
        })
        sg.add_edge(work, E.BY_ARTIST, performer, {"role": "performer"})

    # 작곡·작사 창작자 — MB work artist-rels (저작권 본체의 권리 주체)
    if identity.mb_work_mbid and mb_user_agent:
        for c in MusicBrainzClient(mb_user_agent).work_creators(identity.mb_work_mbid):
            creator = sg.add_node(L.ARTIST, c["mbid"], {
                "name": c.get("name"), "role": c.get("role"),
            })
            sg.add_edge(work, E.BY_ARTIST, creator, {"role": c.get("role")})


def _collect_platforms(sg, work, identity: WorkIdentity, lastfm_api_key) -> None:
    """유통 플랫폼 노드 + 대중성 신호 (Spotify/Deezer/Last.fm).

    ⚠️ Platform 키는 work-scoped(`{platform}:{work_id}`)다 — 지표(rank/playcount/
    popularity)가 *per-work*인데 'deezer' 공유 싱글톤에 저장하면 곡마다 시드가 서로의
    수치를 덮어써(클로버링) 카드가 마지막 시드값을 렌더한다. Artist/Rightsholder/Award는
    진짜 공유 엔티티라 공유하지만, 플랫폼-지표는 '이 곡의 플랫폼 존재'라 곡별로 분리한다.
    """
    wid = work.key

    # popularity(0~100) / rank(0~1M 인기지수) / playcount(재생수) / listeners(청취자).
    # Spotify — 식별만(2026 잠금으로 popularity는 보통 None)
    if identity.spotify_track_id:
        sp = sg.add_node(L.PLATFORM, f"spotify:{wid}", {
            "name": "Spotify", "popularity": identity.spotify_popularity,
        })
        sg.add_edge(work, E.DISTRIBUTED_ON, sp, {
            "popularity": identity.spotify_popularity, "url": identity.spotify_url,
        })

    # Deezer — ISRC로 rank(인기지수, 재생수 아님) 확보 (키 불필요)
    dz = deezer_track_by_isrc(identity.isrc)
    if dz:
        node = sg.add_node(L.PLATFORM, f"deezer:{wid}", {
            "name": "Deezer", "rank": dz.get("rank"),
        })
        sg.add_edge(work, E.DISTRIBUTED_ON, node, {
            "rank": dz.get("rank"), "link": dz.get("link"),
        })

    # Last.fm — listeners/playcount(대중성 + 롱테일 지속성 신호)
    lf = lastfm_track_info(lastfm_api_key, identity.artist, identity.title)
    if lf:
        node = sg.add_node(L.PLATFORM, f"lastfm:{wid}", {
            "name": "Last.fm", "playcount": lf.get("playcount"),
            "listeners": lf.get("listeners"),
        })
        sg.add_edge(work, E.DISTRIBUTED_ON, node, {
            "listeners": lf.get("listeners"), "playcount": lf.get("playcount"),
        })


# ───────────────────────────────────────────────────────────
# 정형 보강 — 웹검색이 못 주는 차트·수상·권리·샘플·소셜을 고신뢰 노드로
# ───────────────────────────────────────────────────────────
def _collect_structured(
    sg, work, identity: WorkIdentity, *,
    mb_user_agent: str | None = None,
    genius_token: str | None = None,
    soundcharts_app_id: str | None = None,
    soundcharts_api_key: str | None = None,
) -> None:
    """Award·Rightsholder·Chart·SocialMetric·커버/샘플 보강 (각 소스 best-effort).

    축별 공백 직격: 수상·샘플/커버(영향력), 차트·소셜(대중성), 권리자(지속성).
    웹 하베스팅(confidence 0.68 정성)과 달리 식별자 기반 정형 소스라 카드가 그대로
    보고서 evidence로 잡힌다(Platform 노드와 동일 패턴).
    """
    # ── Wikidata(무키): 수상 + 권리자(레이블) ──
    wd = wikidata_enrich(
        title=identity.title, artist=identity.artist, iswc=identity.iswc,
        isrc=identity.isrc, mb_work_mbid=identity.mb_work_mbid,
        mb_recording_mbid=identity.mb_recording_mbid, user_agent=mb_user_agent)
    for aw in wd.get("awards", []):
        _add_award(sg, work, aw["award_name"], aw.get("year"), aw.get("result"), aw.get("qid"))
    for rh in wd.get("rightsholders", []):
        _add_rightsholder(sg, work, rh["name"], rh.get("kind"), rh.get("qid"))

    # ── MusicBrainz(UA): 발매 레이블 + 작품 퍼블리셔(권리자) + 파생작 ──
    if mb_user_agent:
        mb = MusicBrainzClient(mb_user_agent)
        for lb in (mb.recording_labels(identity.mb_recording_mbid)
                   + mb.work_publishers(identity.mb_work_mbid)):
            _add_rightsholder(sg, work, lb.get("name"), lb.get("kind"), lb.get("mbid"))

    # ── Genius(키): 샘플/커버(영향력) + 관심도(pageviews) + 사회적 가치(About 설명) ──
    gn = genius_song(identity.title, identity.artist, genius_token)
    for d in gn.get("derivatives", []) + gn.get("covers", []):
        _add_derivative(sg, work, d.get("artist"), d.get("relation"))
    if gn.get("pageviews"):
        _add_metric(sg, work, "Genius", "pageviews", gn["pageviews"], None)
    # 사회적 가치 신호 — Genius 'About'(에디토리얼) 텍스트에서 의제·운동·공적활용 추출.
    _collect_social(sg, work, identity, genius_desc=gn.get("description"),
                    genius_url=gn.get("url"))

    # ── Soundcharts(키): 차트 순위 + 소셜/스트리밍 오디언스 ──
    sc = soundcharts_metrics(identity.isrc, soundcharts_app_id, soundcharts_api_key)
    for ch in sc.get("charts", []):
        _add_chart(sg, work, ch)
    for au in sc.get("audience", []):
        _add_metric(sg, work, au.get("platform"), au.get("metric"),
                    au.get("value"), au.get("as_of"))


def _add_award(sg, work, name, year, result, qid=None) -> None:
    if not name:
        return
    aid = qid or _short(f"{_norm(name)}:{year or ''}")
    node = sg.add_node(L.AWARD, aid, {
        "award_name": name, "year": year, "result": result or "수상"})
    sg.add_edge(work, E.WON, node, {"year": year, "result": result})


def _add_rightsholder(sg, work, name, kind, key=None) -> None:
    if not name:
        return
    rid = key or ("name:" + _norm(name))
    node = sg.add_node(L.RIGHTSHOLDER, rid, {"name": name, "kind": kind})
    sg.add_edge(work, E.HELD_BY, node, {"kind": kind})


def _add_chart(sg, work, ch: dict) -> None:
    name = ch.get("chart_name")
    if not name:
        return
    cid = f"{ch.get('platform') or 'chart'}:{_norm(name)}"
    node = sg.add_node(L.CHART, cid, {
        "chart_name": name, "peak_rank": ch.get("peak_rank"),
        "region": ch.get("region")})
    sg.add_edge(work, E.CHARTED_ON, node, {"peak_rank": ch.get("peak_rank")})


def _add_metric(sg, work, platform, metric, value, as_of) -> None:
    if value is None:
        return
    # work_id를 키에 포함 — 미포함 시 곡마다 다른 pageviews가 같은 노드에 충돌하고
    # 공유 노드가 작품 간 다리가 된다(교차오염).
    mid = _short(f"{work.key}:{platform}:{metric}:{as_of or 'latest'}")
    node = sg.add_node(L.SOCIAL_METRIC, mid, {
        "platform": platform, "metric": metric, "value": value, "as_of": as_of})
    sg.add_edge(work, E.HAS_METRIC, node, {"value": value})


# Genius 'About'은 에디토리얼 출처 — harvester._source_tier의 genius.com과 동일 tier.
_GENIUS_TIER = 0.82


def _collect_social(sg, work, identity: WorkIdentity, *, genius_desc: str | None,
                    genius_url: str | None = None) -> None:
    """가사·에디토리얼 텍스트에서 사회적 가치 신호 추출 → SocialTheme/Cause/PublicUse.

    근거 텍스트(현재 Genius 'About')가 없으면 스킵 — 빈 입력으로 환각 유도 방지. 노드
    키는 work-scoped(곡마다 분리)라 공유 싱글톤 교차오염이 없다(SocialMetric과 동일 전략).
    best-effort: LLM 실패는 빈 결과로 흡수(시드를 막지 않음).

    grounding: social 추출기는 source_excerpt(근거 텍스트 verbatim)가 검증된 항목만
    돌려준다(social._keep_grounded). 여기서는 그 excerpt를 ① typed 노드에 보존하고 ②
    Genius 'About' WebDoc을 EXTRACTED_FROM으로 가리키는 근거 Fact로 적재해, 웹 Fact와
    동일한 감사 체인(Fact→WebDoc, Fact→MENTIONS→Work/typed)을 갖게 한다.
    """
    context = (genius_desc or "").strip()
    if not context:
        return
    from music_copyright.graphflow.social import extract_social_signals
    out = extract_social_signals(
        title=identity.title or "", artist=identity.artist or "", context_text=context)
    title = work.props.get("title")
    # 근거 출처(WebDoc) — Genius 'About' 페이지. 모든 시드 사회신호 Fact의 공통 출처.
    # url_hash 키는 harvester와 동일 스킴이라, 후속 웹검색이 같은 URL을 잡으면 멱등 병합된다.
    webdoc = None
    if genius_url:
        webdoc = sg.add_node(L.WEB_DOC, _short(genius_url), {
            "url": genius_url, "title": f"{identity.title or ''} — Genius",
            "source": "genius.com", "source_tier": _GENIUS_TIER})
    for it in out.themes:
        node = _add_social_theme(sg, work, it.label, it.description, it.source_excerpt, title)
        _ground_social(sg, work, node, it, webdoc)
    for it in out.causes:
        node = _add_cause(sg, work, it.label, it.description, it.source_excerpt)
        _ground_social(sg, work, node, it, webdoc)
    for it in out.public_uses:
        node = _add_public_use(sg, work, it.label, it.description, it.source_excerpt)
        _ground_social(sg, work, node, it, webdoc)
    logger.info("social signals(%s): themes=%d causes=%d public_uses=%d",
                work.key, len(out.themes), len(out.causes), len(out.public_uses))


def _ground_social(sg, work, node, item, webdoc) -> None:
    """사회신호 typed 노드에 근거 Fact를 연결(웹 Fact와 동일 감사 체인)."""
    if node is None:
        return
    text = (item.description or item.label or "").strip()
    if not text:
        return
    # harvester와 동일 키 스킴(work-scoped) — 동일 텍스트 웹 Fact와 멱등 병합 가능.
    fact = sg.add_node(L.FACT, _short(f"{work.key}|{_norm(text)}"), {
        "text": text, "source_excerpt": (item.source_excerpt or "")[:500],
        "source": "genius.com", "source_tier": _GENIUS_TIER,
        "confidence": _GENIUS_TIER, "extraction_method": "genius_about"})
    if fact is None:
        return
    sg.add_edge(fact, E.MENTIONS, work, {})
    sg.add_edge(fact, E.MENTIONS, node, {})
    if webdoc is not None:
        sg.add_edge(fact, E.EXTRACTED_FROM, webdoc, {})


def _add_social_theme(sg, work, label, description, source_excerpt=None,
                      work_title=None):
    if not label:
        return None
    tid = _short(f"{work.key}:theme:{_norm(label)}")
    node = sg.add_node(L.SOCIAL_THEME, tid, {
        "theme": label, "description": description, "work_title": work_title,
        "source_excerpt": (source_excerpt or "")[:500] or None, "source": "genius.com"})
    sg.add_edge(work, E.ADDRESSES, node, {})
    return node


def _add_cause(sg, work, name, description, source_excerpt=None):
    if not name:
        return None
    cid = _short(f"{work.key}:cause:{_norm(name)}")
    node = sg.add_node(L.CAUSE, cid, {
        "name": name, "description": description,
        "source_excerpt": (source_excerpt or "")[:500] or None, "source": "genius.com"})
    sg.add_edge(work, E.SUPPORTED, node, {})
    return node


def _add_public_use(sg, work, context, description, source_excerpt=None):
    if not context:
        return None
    uid = _short(f"{work.key}:use:{_norm(context)}")
    node = sg.add_node(L.PUBLIC_USE, uid, {
        "context": context, "description": description,
        "source_excerpt": (source_excerpt or "")[:500] or None, "source": "genius.com"})
    sg.add_edge(work, E.USED_IN, node, {})
    return node


def _add_derivative(sg, work, artist_name, relation) -> None:
    """커버/샘플한 아티스트 → Artist + COVERED_BY|SAMPLED_BY (이 곡의 영향력 신호).

    role을 자연어로 둬(카드가 'X — 이 곡을 커버한 아티스트'처럼 렌더) 집필 LLM이 엣지
    상수명(SAMPLED_BY)을 그대로 쓰지 않게 한다. 곡명을 박아 카드만으로 관계가 자명.
    """
    if not artist_name:
        return
    is_cover = "cover" in (relation or "")
    title = work.props.get("title") or "이 곡"
    rel_word = "커버" if is_cover else "샘플링"
    aid = "name:" + _norm(artist_name)
    node = sg.add_node(L.ARTIST, aid, {
        "name": artist_name, "role": f"'{title}'을(를) {rel_word}한 아티스트"})
    rel = E.COVERED_BY if is_cover else E.SAMPLED_BY
    sg.add_edge(work, rel, node, {"relation": relation})


# ───────────────────────────────────────────────────────────
# 2단계: Neo4j 적재 (제네릭 MERGE writer) — real_estate와 동형
# ───────────────────────────────────────────────────────────
def _write_nodes(driver: Driver, nodes: list[SeedNode]) -> int:
    """라벨별로 묶어 UNWIND MERGE. card_text 포함, props 통째 SET."""
    by_label: dict[str, list[SeedNode]] = {}
    for n in nodes:
        by_label.setdefault(n.label, []).append(n)
    total = 0
    with driver.session() as session:
        for label, items in by_label.items():
            key_prop = KEY_PROP[label]
            rows = [{"key": n.key,
                     "props": {k: v for k, v in n.props.items() if k != key_prop},
                     "card": n.card_text} for n in items]
            session.run(
                f"""
                UNWIND $rows AS row
                MERGE (n:{label} {{{key_prop}: row.key}})
                SET n += row.props, n.card_text = row.card
                """,
                rows=rows,
            )
            total += len(items)
    return total


def write_edges(driver: Driver, edges: list[SeedEdge]) -> int:
    """(src_label, rel, dst_label)별로 묶어 UNWIND MATCH+MERGE."""
    grouped: dict[tuple, list[SeedEdge]] = {}
    for e in edges:
        grouped.setdefault((e.src_label, e.rel, e.dst_label), []).append(e)
    total = 0
    with driver.session() as session:
        for (src_label, rel, dst_label), items in grouped.items():
            src_key = KEY_PROP[src_label]
            dst_key = KEY_PROP[dst_label]
            rows = [{"src": e.src_key, "dst": e.dst_key, "props": e.props} for e in items]
            session.run(
                f"""
                UNWIND $rows AS row
                MATCH (s:{src_label} {{{src_key}: row.src}})
                MATCH (d:{dst_label} {{{dst_key}: row.dst}})
                MERGE (s)-[r:{rel}]->(d)
                SET r += row.props
                """,
                rows=rows,
            )
            total += len(items)
    return total


def seed(scope: ScopeGraph, driver: Driver, *, embed: bool = False) -> dict:
    """ScopeGraph를 Neo4j에 MERGE 적재. idempotent.

    embed=True면 신규/변경 노드 card_text를 임베딩해 벡터 인덱스까지 채운다.
    기본 False — 빠른 구조 적재만.
    """
    from music_copyright.graphflow.graph.schema import apply_constraints

    apply_constraints(driver)
    n_nodes = _write_nodes(driver, scope.nodes)
    n_edges = write_edges(driver, scope.edges)
    result = {"nodes": n_nodes, "edges": n_edges}

    if embed:
        from music_copyright.graphflow.graph.vector_index import (
            ensure_vector_index,
            update_all_embeddings,
        )
        ensure_vector_index(driver)
        result["embedded"] = update_all_embeddings(driver)

    logger.info("seed DONE: %s", result)
    return result


def seed_from_identity(
    identity: WorkIdentity,
    driver: Driver,
    *,
    lastfm_api_key: str | None = None,
    mb_user_agent: str | None = None,
    genius_token: str | None = None,
    soundcharts_app_id: str | None = None,
    soundcharts_api_key: str | None = None,
    embed: bool = False,
) -> dict:
    """편의 진입점: collect_scope + seed 한 번에."""
    scope = collect_scope(
        identity, lastfm_api_key=lastfm_api_key, mb_user_agent=mb_user_agent,
        genius_token=genius_token, soundcharts_app_id=soundcharts_app_id,
        soundcharts_api_key=soundcharts_api_key)
    out = seed(scope, driver, embed=embed)
    out["scope"] = scope.summary()
    return out
