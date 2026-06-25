"""음악저작물 식별자 해소(인식자) — 자유텍스트(제목+아티스트) → 정규 ID.

real_estate/location.py(LocationResolver/LocationInfo)에 대응하는 음악 도메인 인식자.
부동산이 '주소 → 좌표 → 건물관리번호'로 자산을 확정하듯, 음악은
'제목+아티스트 → ISRC(음원) → ISWC(작품)'로 저작물을 확정한다.

두 클래스:
    1. WorkIdentity   — 해소 결과를 담는 값 객체(필드만, 동작 없음).
    2. WorkResolver   — 외부 API를 호출해 WorkIdentity를 만든다.

해소 체인:
    1. Spotify 검색       → track_id + ISRC(external_ids.isrc) + 정규 제목/아티스트/발매일
    2. MusicBrainz /isrc  → recording MBID + 연결된 Work MBID(performance 관계)
    3. MusicBrainz /work  → ISWC (앵커 확정)
    4. (폴백) MusicBrainz recording 검색 — Spotify에 없는 국내곡/트로트/OST 대응.

MusicBrainz 호출은 music_copyright/external/musicbrainz 공용 클라이언트에 위임한다
(seeder도 같은 클라이언트 사용 — 중복 제거). Spotify는 location.py가 Kakao/Juso를
직접 품듯 여기 내장한다(인식 전용 API).

앵커 식별자(설계 결정):
    - 자산 앵커 = ISWC(작품 = 저작권 본체). 커버/리믹스/라이브는 ISRC가 달라도 ISWC는 하나.
    - 실무 조인 = ISRC(특정 녹음). 다운스트림(스트리밍·차트)은 녹음 단위라 ISRC로 조인.
    - work_id(Work 노드 키) = iswc | 'isrc:'+isrc | 'q:'+해시.

⚠️ 식별만 한다. 대중성·영향력·지속성 지표 보강은 seeder/harvester의 몫이다.
⚠️ 2026-02 Spotify 변경: 신규 앱은 popularity·followers를 못 받는다 — 검색+ISRC 용도로만.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from music_copyright.external.musicbrainz import MusicBrainzClient

logger = logging.getLogger("music_copyright.resolver")

# feat./remaster 등 제목 정규화 시 떼어낼 꼬리표.
_TITLE_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*"
    r"(feat\.?|featuring|with|remaster|remastered|inst\.?|instrumental|"
    r"prod\.?|live|ver\.?|version|edit|mix|remix)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """제목에서 검색 노이즈(feat./remaster/(Live) 등)를 떼어 핵심만 남긴다.

    원본은 WorkIdentity.raw_query에 보존되므로 손실 걱정 없다.
    """
    t = _TITLE_NOISE.sub("", title or "")
    return re.sub(r"\s+", " ", t).strip()


_HANGUL_RE = re.compile(r"[가-힣]")


def _has_hangul(s: str | None) -> bool:
    """문자열에 한글 음절이 있는가 — 표시명 원어(한글) 우선 판정용."""
    return bool(s and _HANGUL_RE.search(s))


# ───────────────────────────────────────────────────────────
# 값 객체 — 해소 결과
# ───────────────────────────────────────────────────────────
@dataclass
class WorkIdentity:
    """음악저작물 식별 결과를 담는 값 객체 (한 번 만들면 그대로 사용).

    필드(가용하면 채워지고, 실패하면 None):
        raw_query / title / artist
        isrc:               국제표준녹음코드 (특정 녹음 — 실무 조인 키)
        iswc:               국제표준작품코드 (작품 — 자산 앵커, 저작권 본체)
        spotify_track_id / spotify_artist_ids / spotify_url / spotify_popularity
        album / release_date / release_year / duration_ms
        mb_recording_mbid / mb_work_mbid
        work_id:            Work 노드 키 = iswc | "isrc:"+isrc | "q:"+해시
        asset_id:           "mc:{work_id}"
        resolution:         해소 경로 메모 (디버깅)
    """

    raw_query: str
    title: str | None = None
    artist: str | None = None
    isrc: str | None = None
    iswc: str | None = None
    spotify_track_id: str | None = None
    spotify_artist_ids: list[str] = field(default_factory=list)
    spotify_url: str | None = None
    spotify_popularity: int | None = None
    album: str | None = None
    release_date: str | None = None
    release_year: int | None = None
    duration_ms: int | None = None
    mb_recording_mbid: str | None = None
    mb_work_mbid: str | None = None
    work_id: str | None = None
    asset_id: str | None = None
    resolution: str = ""

    def __post_init__(self):
        """가용한 가장 강한 식별자로 work_id·asset_id를 확정한다.

        우선순위: ISWC(작품 앵커) > ISRC(녹음) > 질의 해시(최후 폴백).
        """
        if self.iswc:
            self.work_id = self.iswc
        elif self.isrc:
            self.work_id = "isrc:" + self.isrc
        else:
            digest = hashlib.sha1((self.raw_query or "").encode("utf-8")).hexdigest()[:12]
            self.work_id = "q:" + digest
        self.asset_id = "mc:" + self.work_id


# ───────────────────────────────────────────────────────────
# 인식자 — 외부 API로 정규 ID 해소
# ───────────────────────────────────────────────────────────
class WorkResolver:
    """자유 형식 음악 질의를 외부 API로 해소하여 WorkIdentity로 반환한다.

    사용법:
        resolver = WorkResolver(
            spotify_client_id="...", spotify_client_secret="...",
            musicbrainz_user_agent="STO-Valuation/1.0 ( contact )")
        ident = resolver.resolve(title="좋니", artist="윤종신")

    설계: ISWC/MB 해소 실패는 치명적이지 않다(None으로 두고 진행). Spotify·MB 둘 다
    곡을 못 찾을 때만 LookupError.
    """

    SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
    SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"

    def __init__(
        self,
        spotify_client_id: str,
        spotify_client_secret: str,
        musicbrainz_user_agent: str,
        *,
        market: str = "KR",
        timeout: float = 15.0,
    ):
        self.spotify_client_id = spotify_client_id
        self.spotify_client_secret = spotify_client_secret
        self.market = market
        self.timeout = timeout
        # MusicBrainz는 공용 클라이언트에 위임(레이트리밋 상태도 여기 한 곳).
        self._mb = MusicBrainzClient(musicbrainz_user_agent, timeout=timeout)
        # Spotify 토큰 캐시
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # ── 공개 진입점 ──────────────────────────────────────────
    def resolve(self, title: str, artist: str | None = None) -> WorkIdentity:
        """제목(+아티스트)을 받아 해소된 WorkIdentity를 반환한다.

        Raises:
            LookupError: Spotify·MusicBrainz 모두에서 곡을 못 찾은 경우.
        """
        raw_query = " ".join(x for x in (artist, title) if x).strip()
        norm_title = normalize_title(title)

        # 1단계: Spotify 검색 (1차 로케이터)
        sp = self._spotify_search(norm_title, artist)
        if sp is not None:
            isrc = sp.get("isrc")
            mb = self._mb.lookup_by_isrc(isrc) if isrc else None
            if mb is None:                       # ISRC 없음/MB 미등록 → 제목·아티스트 폴백
                mb = self._mb.search_recording(
                    sp.get("title") or norm_title, sp.get("artist") or artist)
            return self._build(raw_query, sp, mb, resolution="spotify",
                               in_title=title, in_artist=artist)

        # 2순위: Spotify 실패 → MusicBrainz recording 검색 폴백(국내곡 대응)
        mb = self._mb.search_recording(norm_title, artist)
        if mb is not None:
            return self._build(raw_query, None, mb, resolution="musicbrainz_fallback",
                               in_title=title, in_artist=artist)

        raise LookupError(f"곡을 찾을 수 없습니다: {raw_query!r}")

    # ── 결과 조립 ────────────────────────────────────────────
    def _build(self, raw_query, sp, mb, *, resolution, in_title=None, in_artist=None) -> WorkIdentity:
        sp = sp or {}
        mb = mb or {}
        release_date = sp.get("release_date")
        # 표시명: 입력이 한글이면 원어 유지 — Spotify는 K-pop 제목/아티스트를 로마자로
        # 저장해(좋니→"Like it", 윤종신→"Yoon Jong Shin") 보고서가 영어로 나온다. 식별자
        # (ISRC/ISWC/MBID)는 그대로 Spotify·MB에서 받되, 사람이 읽는 이름만 원어 우선.
        title = (in_title if _has_hangul(in_title) else None) or sp.get("title") or mb.get("title") or in_title
        artist = (in_artist if _has_hangul(in_artist) else None) or sp.get("artist") or mb.get("artist") or in_artist
        return WorkIdentity(
            raw_query=raw_query,
            title=title,
            artist=artist,
            isrc=sp.get("isrc") or mb.get("isrc"),
            iswc=mb.get("iswc"),
            spotify_track_id=sp.get("track_id"),
            spotify_artist_ids=sp.get("artist_ids", []),
            spotify_url=sp.get("url"),
            spotify_popularity=sp.get("popularity"),
            album=sp.get("album"),
            release_date=release_date,
            release_year=_year_of(release_date),
            duration_ms=sp.get("duration_ms"),
            mb_recording_mbid=mb.get("recording_mbid"),
            mb_work_mbid=mb.get("work_mbid"),
            resolution=resolution,
        )

    # ── Spotify (인식 전용 — location.py가 Kakao/Juso 내장하듯 여기 내장) ──
    def _spotify_token(self) -> str:
        """client-credentials 토큰을 발급/캐시한다."""
        now = time.monotonic()
        if self._token and now < self._token_expiry:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        creds = f"{self.spotify_client_id}:{self.spotify_client_secret}".encode()
        req = urllib.request.Request(self.SPOTIFY_TOKEN_URL, data=body, method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(creds).decode())
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self._token = data["access_token"]
        self._token_expiry = now + float(data.get("expires_in", 3600)) - 60
        return self._token

    def _spotify_search(self, title: str, artist: str | None) -> dict | None:
        """Spotify 트랙 검색 → 첫 결과 정규 메타 + ISRC. 실패 시 None (폴백으로 흡수).

        2026-02 변경으로 limit 최대 10. popularity는 신규 앱에서 None일 수 있다.
        """
        if not title:
            return None
        q = f'track:"{title}"'
        if artist:
            q += f' artist:"{artist}"'
        params = urllib.parse.urlencode({
            "q": q, "type": "track", "market": self.market, "limit": 10})
        url = f"{self.SPOTIFY_SEARCH_URL}?{params}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + self._spotify_token())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Spotify 검색 실패(폴백 진행): %s", exc)
            return None
        items = (data.get("tracks") or {}).get("items") or []
        if not items:
            return None
        t = items[0]
        artists = t.get("artists") or []
        album = t.get("album") or {}
        return {
            "track_id": t.get("id"),
            "title": t.get("name"),
            "artist": ", ".join(a.get("name", "") for a in artists) or None,
            "artist_ids": [a.get("id") for a in artists if a.get("id")],
            "isrc": (t.get("external_ids") or {}).get("isrc"),
            "url": (t.get("external_urls") or {}).get("spotify"),
            "popularity": t.get("popularity"),
            "album": album.get("name"),
            "release_date": album.get("release_date"),
            "duration_ms": t.get("duration_ms"),
        }


# ───────────────────────────────────────────────────────────
# 모듈 헬퍼
# ───────────────────────────────────────────────────────────
def _year_of(release_date: str | None) -> int | None:
    """발매일 문자열("YYYY-MM-DD"/"YYYY")에서 연도(int)를 추출."""
    if not release_date:
        return None
    m = re.match(r"(\d{4})", release_date)
    return int(m.group(1)) if m else None
