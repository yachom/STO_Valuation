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
        """ISRC → recording MBID + 연결 Work MBID + ISWC + 제목/아티스트 별칭(다국어)."""
        if not isrc:
            return None
        data = self._get(f"isrc/{urllib.parse.quote(isrc)}",
                         {"inc": "work-rels+artist-credits+aliases"})
        recordings = (data or {}).get("recordings") or []
        if not recordings:
            return None
        rec = recordings[0]
        work_mbid = _first_work_mbid(rec)
        artist_mbid = _first_artist_mbid(rec)
        return {
            "isrc": isrc,
            "recording_mbid": rec.get("id"),
            "title": rec.get("title"),
            "artist": _artist_credit_name(rec),
            "artist_mbid": artist_mbid,
            "title_aliases": _alias_names(rec),
            "artist_aliases": self.aliases("artist", artist_mbid),
            "work_mbid": work_mbid,
            "iswc": self.work_iswc(work_mbid),
        }

    def aliases(self, entity: str, mbid: str | None) -> list[str]:
        """엔티티(artist/work/recording) MBID → 다국어 별칭 이름 목록(원어·로마자 등).

        K-pop처럼 Spotify는 로마자, 국내 보도는 원어를 쓰는 경우의 음차 불일치를 메운다.
        best-effort: 실패/부재 시 []."""
        if not mbid:
            return []
        data = self._get(f"{entity}/{mbid}", {"inc": "aliases"})
        return _alias_names(data or {})

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
                           {"inc": "work-rels+isrcs+artist-credits+aliases"}) or recs[0]
        work_mbid = _first_work_mbid(detail)
        artist_mbid = _first_artist_mbid(detail)
        return {
            "isrc": (detail.get("isrcs") or [None])[0],
            "recording_mbid": rec_id,
            "title": detail.get("title"),
            "artist": _artist_credit_name(detail),
            "artist_mbid": artist_mbid,
            "title_aliases": _alias_names(detail),
            "artist_aliases": self.aliases("artist", artist_mbid),
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

    # ── 권리자·파생작 (seeder 구조 보강) ─────────────────────
    def work_publishers(self, work_mbid: str | None) -> list[dict]:
        """작품 MBID → 퍼블리셔/레이블 권리자 [{mbid, name, kind}] (work label-rels).

        작품 레벨 퍼블리싱 관계. 많은 작품은 비어있고(레이블은 보통 release 레벨),
        그 경우 recording_labels로 보완한다.
        """
        if not work_mbid:
            return []
        data = self._get(f"work/{work_mbid}", {"inc": "label-rels"})
        out: list[dict] = []
        for rel in (data or {}).get("relations") or []:
            label = rel.get("label")
            if label and label.get("id"):
                out.append({"mbid": label["id"], "name": label.get("name"),
                            "kind": rel.get("type") or "퍼블리셔"})
        return out

    def recording_labels(self, recording_mbid: str | None, *, max_releases: int = 3) -> list[dict]:
        """녹음 MBID → 발매 레이블 [{mbid, name, kind}] (recording→releases→label-info).

        레이블은 release에 달려 있어 recording의 releases를 받고 앞쪽 몇 개의 label-info를
        모은다(dedup). 권리 카탈로그(Rightsholder) 신호 — Wikidata가 비는 국내곡 보완.
        """
        if not recording_mbid:
            return []
        data = self._get(f"recording/{recording_mbid}",
                         {"inc": "releases", "limit": max_releases})
        out: list[dict] = []
        seen: set[str] = set()
        for rel in ((data or {}).get("releases") or [])[:max_releases]:
            rid = rel.get("id")
            if not rid:
                continue
            rdata = self._get(f"release/{rid}", {"inc": "labels"})
            for li in (rdata or {}).get("label-info") or []:
                label = li.get("label")
                if label and label.get("id") and label["id"] not in seen:
                    seen.add(label["id"])
                    out.append({"mbid": label["id"], "name": label.get("name"),
                                "kind": "레이블"})
        return out

    def work_derivatives(self, work_mbid: str | None) -> list[dict]:
        """작품 MBID → 파생작(커버·샘플·메들리·다른 버전) [{mbid, title, relation}].

        work-work 관계(based on/samples material/medley/other version 등). 이 곡이
        다른 작품에 미친 영향(영향력 축) 신호. 없으면 [](MB는 유명·서구곡에 풍부).
        """
        if not work_mbid:
            return []
        data = self._get(f"work/{work_mbid}", {"inc": "work-rels"})
        out: list[dict] = []
        for rel in (data or {}).get("relations") or []:
            w = rel.get("work")
            if w and w.get("id"):
                out.append({"mbid": w["id"], "title": w.get("title"),
                            "relation": rel.get("type"),
                            "direction": rel.get("direction")})
        return out


# ── 모듈 헬퍼 ────────────────────────────────────────────────
def _first_work_mbid(recording: dict) -> str | None:
    """recording.relations에서 첫 work(보통 type='performance') MBID."""
    for rel in recording.get("relations") or []:
        work = rel.get("work")
        if work and work.get("id"):
            return work["id"]
    return None


def _first_artist_mbid(recording: dict) -> str | None:
    """recording.artist-credit에서 첫 아티스트 MBID(별칭 조회용)."""
    for c in recording.get("artist-credit") or []:
        if isinstance(c, dict):
            a = c.get("artist") or {}
            if a.get("id"):
                return a["id"]
    return None


def _alias_names(entity: dict) -> list[str]:
    """엔티티의 aliases 배열 → 이름 목록(중복 제거, 순서 보존)."""
    out: list[str] = []
    for a in entity.get("aliases") or []:
        name = a.get("name") if isinstance(a, dict) else None
        if name and name not in out:
            out.append(name)
    return out


def _artist_credit_name(recording: dict) -> str | None:
    """recording.artist-credit → 아티스트명 조합."""
    names = []
    for c in recording.get("artist-credit") or []:
        if isinstance(c, dict):
            name = c.get("name") or (c.get("artist") or {}).get("name")
            if name:
                names.append(name)
    return "".join(names) or None
