"""Deezer 공개 카탈로그 — ISRC 트랙 조회 (키 불필요).

rank는 Deezer 인기지수(0~1,000,000 척도, 재생수 아님) — Spotify가 못 주는 대중성
신호 대체. best-effort.
"""

from __future__ import annotations

import urllib.parse

from music_copyright.external._http import get_json


def deezer_track_by_isrc(isrc: str | None) -> dict | None:
    """ISRC → Deezer 트랙 {deezer_id, title, rank, artist, album, duration, link}. 실패 시 None."""
    if not isrc:
        return None
    data = get_json(f"https://api.deezer.com/track/isrc:{urllib.parse.quote(isrc)}")
    if not data or data.get("error") or not data.get("id"):
        return None
    return {
        "deezer_id": data.get("id"),
        "title": data.get("title"),
        "rank": data.get("rank"),
        "artist": (data.get("artist") or {}).get("name"),
        "album": (data.get("album") or {}).get("title"),
        "duration": data.get("duration"),
        "link": data.get("link"),
    }
