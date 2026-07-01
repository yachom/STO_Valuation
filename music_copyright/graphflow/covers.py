"""유튜브 커버 인식 (ddgs videos) → COVERED_BY 적재 — C2 세대전이 longevity 신호.

MB/Genius 파생작이 못 잡는 **유튜브 롱테일 커버**를 ddgs 비디오 검색으로 수집한다(실측: ddgs
videos 결과 대부분이 YouTube). 비디오 제목·설명에 커버 아티스트가 노출되므로 LLM이 *진짜
커버*만 추출해(리액션·튜토리얼·노래방·AI리메이크·원곡자 제외) Work-COVERED_BY->Artist로 적재.
`published`(업로드일)로 커버 **연도**를 달아 decade-span 계산의 근거를 만든다.

firewall 무관(커버=지속성·영향력 신호이지 상업 성과가 아님). 외부호출은 DdgsClient(게이트웨이·
audit·Slack 웹훅 경유). 라이브 Neo4j 직접 write(seeder ScopeGraph 아님), 멱등(MERGE)·best-effort.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import (
    BaseAgent,
    music_system_prompt,
    normalize_text,
)

logger = logging.getLogger("music_copyright.covers")


class _Cover(BaseModel):
    artist: str = Field(
        description="커버한 아티스트/채널명 (원곡 아티스트 제외). 제목·설명·업로더에서 추출.")
    is_genuine_cover: bool = Field(
        description="실제 커버 연주·가창이면 true. 리액션·튜토리얼·노래방(MR/인스트)·AI 리메이크·"
                    "원곡 공식 영상·원곡 아티스트 본인 무대는 false.")


class _CoversOut(BaseModel):
    covers: list[_Cover] = Field(default_factory=list)


class CoverExtractor(BaseAgent):
    """비디오 제목·설명에서 진짜 커버 아티스트를 추출(gpt-4o-mini 티어 재사용)."""

    name = "music_harvester"

    def extract(self, orig_artist: str, orig_title: str, videos: list[dict]) -> _CoversOut:
        if not videos:
            return _CoversOut()
        lines = []
        for i, v in enumerate(videos):
            desc = (v.get("description") or "")[:160]
            lines.append(f"{i + 1}. 제목: {v.get('title', '')}\n"
                         f"   업로더: {v.get('uploader', '')}\n   설명: {desc}")
        user = (
            f"[원곡] {orig_title} — {orig_artist}\n[유튜브 비디오]\n" + "\n".join(lines)
            + "\n\n각 비디오가 이 곡의 *실제 커버*인지 판정하고 커버 아티스트명을 추출하세요. "
            "원곡 아티스트 본인·공식 MV·리액션·튜토리얼·노래방(MR)·AI 리메이크는 커버가 아닙니다"
            "(is_genuine_cover=false). 아티스트명이 제목에 없으면 업로더 채널명을 쓰되, 그마저 "
            "불명확하면 제외하세요. 순서와 개수는 입력 비디오와 무관하게 실제 커버만 반환.")
        try:
            return self.complete(system=music_system_prompt(""), user=user, schema=_CoversOut)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("cover extract 실패: %s", exc)
            return _CoversOut()


def _year(published) -> int | None:
    """ddgs published("2026-02-05T…") → 연도. 없거나 이상하면 None."""
    if not isinstance(published, str) or len(published) < 4:
        return None
    try:
        y = int(published[:4])
        return y if 1900 <= y <= 2100 else None
    except ValueError:
        return None


def collect_video_covers(driver, work_id: str, *, max_videos: int = 10,
                         region: str = "kr-ko") -> dict:
    """ddgs 비디오로 유튜브 커버 인식 → Work-COVERED_BY->Artist(+cover_year) 적재.

    커버 연도 span(min~max)을 함께 산출해 C2 세대전이 longevity의 근거로 남긴다. 멱등(MERGE)·
    best-effort. 반환: {videos_seen, covers_added, year_span}.
    """
    from infra.external.ddgs.client import DdgsClient
    from music_copyright.graphflow.graph.cards import build_card
    from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
    from music_copyright.graphflow.graph.schema import KEY_PROP
    from music_copyright.graphflow.graph.schema import NODE_LABELS as L

    stats = {"videos_seen": 0, "covers_added": 0, "year_span": None}
    wk, ak = KEY_PROP[L.WORK], KEY_PROP[L.ARTIST]
    try:
        with driver.session() as s:
            ctx = s.run(
                f"MATCH (w:{L.WORK} {{{wk}: $wid}}) "
                f"OPTIONAL MATCH (w)-[:{E.BY_ARTIST}]->(a:{L.ARTIST}) "
                "RETURN w.title AS title, coalesce(a.name, a.artist_name) AS artist LIMIT 1",
                wid=work_id).single()
        if ctx is None or not (ctx["title"] and ctx["artist"]):
            return stats
        title, artist = ctx["title"], ctx["artist"]

        # 날짜 spread 확보를 위해 timelimit 미지정(전 기간) — published로 연도 span 계산.
        videos = DdgsClient().videos(f"{artist} {title} cover",
                                     max_results=max_videos, region=region)
        stats["videos_seen"] = len(videos)
        if not videos:
            return stats

        out = CoverExtractor().extract(artist, title, videos)
        # published/url 근거를 붙이기 위해 비디오와 정렬 매칭(제목 부분일치).
        by_title = {(v.get("title") or "").strip(): v for v in videos}
        artist_norm = normalize_text(artist)
        years: list[int] = []
        with driver.session() as s:
            for c in out.covers:
                cov = (c.artist or "").strip()
                if not (c.is_genuine_cover and cov) or normalize_text(cov) == artist_norm:
                    continue
                # 근거 비디오 찾기(아티스트명이 제목/업로더에 담긴 것 우선).
                src = next((v for v in videos
                            if normalize_text(cov) in normalize_text(
                                f"{v.get('title', '')} {v.get('uploader', '')}")), None)
                yr = _year((src or {}).get("published"))
                if yr:
                    years.append(yr)
                akey = "name:" + normalize_text(cov)
                props = {"name": cov, "role": f"'{title}'을(를) 커버한 아티스트", "via": "ddgs_video"}
                card = build_card(L.ARTIST, {**props, ak: akey})
                s.run(f"MERGE (n:{L.ARTIST} {{{ak}: $k}}) "
                      "SET n += $p, n.card_text = coalesce(n.card_text, $c)",
                      k=akey, p=props, c=card)
                epr: dict = {"relation": "cover", "via": "ddgs_video"}
                if src:
                    epr["source"] = src.get("url")
                    if src.get("view_count"):
                        epr["view_count"] = src.get("view_count")
                if yr:
                    epr["cover_year"] = yr
                s.run(f"MATCH (w:{L.WORK} {{{wk}: $wid}}) MATCH (n:{L.ARTIST} {{{ak}: $k}}) "
                      f"MERGE (w)-[r:{E.COVERED_BY}]->(n) SET r += $ep",
                      wid=work_id, k=akey, ep=epr)
                stats["covers_added"] += 1
        if years:
            stats["year_span"] = [min(years), max(years)]
    except Exception as exc:  # noqa: BLE001 — best-effort: 커버 수집 실패가 보고서를 막지 않음
        logger.warning("collect_video_covers(%s) 실패(계속): %s", work_id, exc)
    logger.info("collect_video_covers(%s): videos=%d covers=%d span=%s",
                work_id, stats["videos_seen"], stats["covers_added"], stats["year_span"])
    return stats
