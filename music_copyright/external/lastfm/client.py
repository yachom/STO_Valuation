"""Last.fm — track.getInfo (listeners/playcount).

listeners(청취자 수)·playcount(누적 재생)는 대중성 + 롱테일 지속성 신호. api_key 필요.
best-effort. (상업 제품은 별도 라이선스 협약 필요 — 약관 확인.)
"""

from __future__ import annotations

import urllib.parse

from music_copyright.external._http import get_json


def lastfm_track_info(api_key: str | None, artist: str | None, track: str | None) -> dict | None:
    """artist+track → {listeners, playcount, url}. 실패/부재 시 None."""
    if not (api_key and artist and track):
        return None
    params = urllib.parse.urlencode({
        "method": "track.getInfo", "api_key": api_key,
        "artist": artist, "track": track, "format": "json", "autocorrect": 1,
    })
    data = get_json(f"http://ws.audioscrobbler.com/2.0/?{params}")
    t = (data or {}).get("track")
    if not t:
        return None
    return {
        "listeners": _to_int(t.get("listeners")),
        "playcount": _to_int(t.get("playcount")),
        "url": t.get("url"),
    }


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
