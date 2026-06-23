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

import logging
import re
from dataclasses import dataclass, field

from neo4j import Driver

from music_copyright.graphflow.graph.cards import build_card
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.external.deezer import deezer_track_by_isrc
from music_copyright.external.lastfm import lastfm_track_info
from music_copyright.external.musicbrainz import MusicBrainzClient
from music_copyright.resolver import WorkIdentity

logger = logging.getLogger("music_copyright.seeder")


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
) -> ScopeGraph:
    """WorkIdentity → 자산 스코프 ScopeGraph.

    lastfm_api_key / mb_user_agent가 None이면 해당 소스 보강을 건너뛴다(graceful).
    Work 앵커 + 연주/창작 Artist + 유통 Platform(Spotify/Deezer/Last.fm)을 모은다.
    """
    sg = ScopeGraph()

    work = sg.add_node(L.WORK, identity.work_id, {
        "title": identity.title,
        "artist_name": identity.artist,
        "release_year": identity.release_year,
        "iswc": identity.iswc,
        "isrc": identity.isrc,
        "spotify_track_id": identity.spotify_track_id,
        "album": identity.album,
    })

    _collect_artists(sg, work, identity, mb_user_agent)
    _collect_platforms(sg, work, identity, lastfm_api_key)

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
    """유통 플랫폼 노드 + 대중성 신호 (Spotify/Deezer/Last.fm)."""
    # 노드 prop은 지표 의미대로 분리 저장한다(카드가 정확히 렌더 → 사실오류 차단):
    # popularity(0~100) / rank(0~1M 인기지수) / playcount(재생수) / listeners(청취자).
    # Spotify — 식별만(2026 잠금으로 popularity는 보통 None)
    if identity.spotify_track_id:
        sp = sg.add_node(L.PLATFORM, "spotify", {
            "name": "Spotify", "popularity": identity.spotify_popularity,
        })
        sg.add_edge(work, E.DISTRIBUTED_ON, sp, {
            "popularity": identity.spotify_popularity, "url": identity.spotify_url,
        })

    # Deezer — ISRC로 rank(인기지수, 재생수 아님) 확보 (키 불필요)
    dz = deezer_track_by_isrc(identity.isrc)
    if dz:
        node = sg.add_node(L.PLATFORM, "deezer", {
            "name": "Deezer", "rank": dz.get("rank"),
        })
        sg.add_edge(work, E.DISTRIBUTED_ON, node, {
            "rank": dz.get("rank"), "link": dz.get("link"),
        })

    # Last.fm — listeners/playcount(대중성 + 롱테일 지속성 신호)
    lf = lastfm_track_info(lastfm_api_key, identity.artist, identity.title)
    if lf:
        node = sg.add_node(L.PLATFORM, "lastfm", {
            "name": "Last.fm", "playcount": lf.get("playcount"),
            "listeners": lf.get("listeners"),
        })
        sg.add_edge(work, E.DISTRIBUTED_ON, node, {
            "listeners": lf.get("listeners"), "playcount": lf.get("playcount"),
        })


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
    embed: bool = False,
) -> dict:
    """편의 진입점: collect_scope + seed 한 번에."""
    scope = collect_scope(identity, lastfm_api_key=lastfm_api_key, mb_user_agent=mb_user_agent)
    out = seed(scope, driver, embed=embed)
    out["scope"] = scope.summary()
    return out
