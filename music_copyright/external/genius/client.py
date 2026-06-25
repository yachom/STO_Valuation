"""Genius 커넥터 — 크레딧·프로듀서·샘플/인터폴레이션/커버 보강 (무료 키).

웹검색이 못 주는 *관계형* 영향력 신호를 Genius API에서 끌어온다:
    - song_relationships(samples/interpolates/sampled_in/cover_of/covered_by)
        → SAMPLED_BY / COVERED_BY (영향력 — 이 곡이 파생시킨/참조한 작품)
    - producer_artists / writer_artists  → Artist 노드 보강(창작 기여)
    - stats.pageviews                    → SocialMetric(대중 관심도)

키: GENIUS_ACCESS_TOKEN (genius.com/api-clients 에서 무료 발급). 누락이면 호출부가
스킵한다. 모든 호출 best-effort — 실패/부재는 빈 결과로 흡수.

레퍼런스: docs.genius.com (GET /search, GET /songs/{id}).
"""

from __future__ import annotations

import logging
import urllib.parse

from music_copyright.external._http import get_json

logger = logging.getLogger("music_copyright.external.genius")

_BASE = "https://api.genius.com"
# Genius song_relationships.relationship_type → (출력 버킷, 방향성 의미)
_SAMPLE_RELS = {"samples", "interpolates"}          # 이 곡이 남을 차용 (피영향)
_DERIV_RELS = {"sampled_in", "covered_by", "remixed_by"}  # 남이 이 곡을 차용 (영향력↑)
_COVER_RELS = {"cover_of", "covered_by"}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _search_song_id(title: str, artist: str | None, token: str) -> int | None:
    """제목(+아티스트) 검색 → primary_artist가 artist와 맞는 첫 song id."""
    q = f"{title} {artist}".strip() if artist else title
    data = get_json(f"{_BASE}/search?{urllib.parse.urlencode({'q': q})}", _auth(token))
    hits = (data or {}).get("response", {}).get("hits", [])
    if not hits:
        return None
    if not artist:
        return (hits[0].get("result") or {}).get("id")
    a = artist.lower()
    for h in hits:
        res = h.get("result") or {}
        pname = ((res.get("primary_artist") or {}).get("name") or "").lower()
        if a in pname or pname in a:
            return res.get("id")
    return (hits[0].get("result") or {}).get("id")


def genius_song(title: str | None, artist: str | None, token: str | None) -> dict:
    """Genius 보강. {song_id, pageviews, producers, writers, samples, derivatives, covers}.

    samples=이 곡이 차용한 작품, derivatives=이 곡을 차용/커버한 작품(영향력),
    covers=커버 관계. 토큰/제목 없으면 {} (graceful).
    """
    if not (title and token):
        return {}
    sid = _search_song_id(title, artist, token)
    if not sid:
        logger.info("genius: song 미해소 (%s)", title)
        return {}
    data = get_json(f"{_BASE}/songs/{sid}?text_format=plain", _auth(token))
    song = (data or {}).get("response", {}).get("song")
    if not song:
        return {}

    def _people(key):
        return [{"name": a.get("name"), "genius_id": a.get("id")}
                for a in (song.get(key) or []) if a.get("name")]

    samples, derivatives, covers = [], [], []
    for rel in song.get("song_relationships") or []:
        rtype = rel.get("relationship_type")
        for s in rel.get("songs") or []:
            entry = {"title": s.get("full_title") or s.get("title"),
                     "genius_id": s.get("id"), "relation": rtype,
                     "artist": (s.get("primary_artist") or {}).get("name")}
            if rtype in _SAMPLE_RELS:
                samples.append(entry)
            elif rtype in _DERIV_RELS:
                derivatives.append(entry)
            if rtype in _COVER_RELS:
                covers.append(entry)

    out = {
        "song_id": sid,
        "pageviews": ((song.get("stats") or {}).get("pageviews")),
        "producers": _people("producer_artists"),
        "writers": _people("writer_artists"),
        "samples": samples,
        "derivatives": derivatives,
        "covers": covers,
        "url": song.get("url"),
    }
    logger.info("genius %s: producers=%d writers=%d samples=%d deriv=%d pv=%s", sid,
                len(out["producers"]), len(out["writers"]), len(samples),
                len(derivatives), out["pageviews"])
    return out
