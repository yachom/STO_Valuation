"""MusicBrainz 클라이언트 — resolver(식별)와 seeder(창작자)가 공유하는 단일 진입.

이전엔 resolver.py와 sources.py가 MusicBrainz 호출을 각자 중복 구현했다. 둘을 여기로
통합한다(레이트리밋 상태도 한 인스턴스에 모임). User-Agent 필수, 익명 1 req/s.

메서드:
    lookup_by_isrc(isrc)        → {recording_mbid, title, work_mbid, iswc}
    search_recording(title,art) → {isrc, recording_mbid, title, artist, work_mbid, iswc}
    work_iswc(work_mbid)        → ISWC str | None
    work_creators(work_mbid)    → [{mbid, name, role}]  (작곡·작사 등)

전부 best-effort: 실패/부재는 None/빈값으로 흡수.
"""

from __future__ import annotations

import logging
import time
import urllib.parse

from music_copyright.external._http import get_json

logger = logging.getLogger("music_copyright.external.musicbrainz")

_MIN_INTERVAL_S = 1.1  # 익명 1 req/s 준수


class MusicBrainzClient:
    BASE = "https://musicbrainz.org/ws/2"

    def __init__(self, user_agent: str, *, timeout: float = 15.0):
        self.user_agent = user_agent
        self.timeout = timeout
        self._last_call = 0.0

    def _get(self, path: str, params: dict) -> dict | None:
        # 1 req/s: 직전 호출과 최소 간격 보장.
        wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        params = {**params, "fmt": "json"}
        url = f"{self.BASE}/{path}?{urllib.parse.urlencode(params)}"
        try:
            return get_json(url, {"User-Agent": self.user_agent,
                                  "Accept": "application/json"}, self.timeout)
        finally:
            self._last_call = time.monotonic()

    # ── 식별 (resolver) ──────────────────────────────────────
    def lookup_by_isrc(self, isrc: str | None) -> dict | None:
        """ISRC → recording MBID + 연결 Work MBID + ISWC."""
        if not isrc:
            return None
        data = self._get(f"isrc/{urllib.parse.quote(isrc)}", {"inc": "work-rels"})
        recordings = (data or {}).get("recordings") or []
        if not recordings:
            return None
        rec = recordings[0]
        work_mbid = _first_work_mbid(rec)
        return {
            "isrc": isrc,
            "recording_mbid": rec.get("id"),
            "title": rec.get("title"),
            "work_mbid": work_mbid,
            "iswc": self.work_iswc(work_mbid),
        }

    def search_recording(self, title: str | None, artist: str | None) -> dict | None:
        """제목/아티스트로 recording 검색 → 상세 재조회(work·isrc·artist). 국내곡 폴백."""
        if not title:
            return None
        q = f'recording:"{title}"'
        if artist:
            q += f' AND artist:"{artist}"'
        data = self._get("recording", {"query": q, "limit": 5})
        recs = (data or {}).get("recordings") or []
        if not recs:
            return None
        rec_id = recs[0].get("id")
        if not rec_id:
            return None
        detail = self._get(f"recording/{rec_id}",
                           {"inc": "work-rels+isrcs+artist-credits"}) or recs[0]
        work_mbid = _first_work_mbid(detail)
        return {
            "isrc": (detail.get("isrcs") or [None])[0],
            "recording_mbid": rec_id,
            "title": detail.get("title"),
            "artist": _artist_credit_name(detail),
            "work_mbid": work_mbid,
            "iswc": self.work_iswc(work_mbid),
        }

    def work_iswc(self, work_mbid: str | None) -> str | None:
        """Work MBID → ISWC. 실패/부재 시 None."""
        if not work_mbid:
            return None
        data = self._get(f"work/{work_mbid}", {})
        iswcs = (data or {}).get("iswcs") or []
        return iswcs[0] if iswcs else None

    # ── 창작자 (seeder) ──────────────────────────────────────
    def work_creators(self, work_mbid: str | None) -> list[dict]:
        """작품 MBID → 창작자 [{mbid, name, role}]. role=composer/lyricist 등. 없으면 []."""
        if not work_mbid:
            return []
        data = self._get(f"work/{work_mbid}", {"inc": "artist-rels"})
        out: list[dict] = []
        for rel in (data or {}).get("relations") or []:
            artist = rel.get("artist")
            if artist and artist.get("id"):
                out.append({"mbid": artist["id"], "name": artist.get("name"),
                            "role": rel.get("type")})
        return out


# ── 모듈 헬퍼 ────────────────────────────────────────────────
def _first_work_mbid(recording: dict) -> str | None:
    """recording.relations에서 첫 work(보통 type='performance') MBID."""
    for rel in recording.get("relations") or []:
        work = rel.get("work")
        if work and work.get("id"):
            return work["id"]
    return None


def _artist_credit_name(recording: dict) -> str | None:
    """recording.artist-credit → 아티스트명 조합."""
    names = []
    for c in recording.get("artist-credit") or []:
        if isinstance(c, dict):
            name = c.get("name") or (c.get("artist") or {}).get("name")
            if name:
                names.append(name)
    return "".join(names) or None
