"""Soundcharts 커넥터 — 차트·스트리밍·소셜 오디언스 집계 (유료 키).

웹검색·Wikidata가 못 주는 *정형 시계열* 지표를 한 곳에서 끌어온다(차트+스트리밍+소셜):
    - charts/ranking/latest  → Chart 노드 (피크 순위·차트명·지역 — 대중성/영향력)
    - audience/{platform}    → SocialMetric 노드 (팔로워·청취자·조회 — 대중성/팬덤)

키: SOUNDCHARTS_APP_ID + SOUNDCHARTS_API_KEY (헤더 x-app-id / x-api-key). 둘 다 있어야
활성, 누락이면 호출부가 스킵. 샌드박스: app-id/api-key 모두 "soundcharts", 테스트
ISRC "USUG11904206".

⚠️ 엔드포인트 버전/플랜 의존: 아래 경로는 공개 문서 기준 best-effort다. 실제 키 발급 후
응답 스키마(items[].position / .chart.name 등)를 한 번 검증하고, 필요시 _BASE·버전만
조정한다. 모든 호출은 실패/부재를 빈 결과로 흡수.

레퍼런스: doc.soundcharts.com (Song endpoints).
"""

from __future__ import annotations

import logging
import urllib.parse

from music_copyright.external._http import get_json

logger = logging.getLogger("music_copyright.external.soundcharts")

_BASE = "https://customer.api.soundcharts.com"
# 소셜/스트리밍 오디언스를 끌어올 플랫폼(가용한 것만 best-effort).
_AUDIENCE_PLATFORMS = ("spotify", "youtube", "tiktok", "instagram")


def _headers(app_id: str, api_key: str) -> dict:
    return {"x-app-id": app_id, "x-api-key": api_key, "Accept": "application/json"}


def _get(path: str, app_id: str, api_key: str) -> dict | None:
    return get_json(f"{_BASE}{path}", _headers(app_id, api_key))


def _song_uuid(isrc: str, app_id: str, api_key: str) -> str | None:
    """ISRC → Soundcharts song UUID (조인 키). 첫 매칭."""
    data = _get(f"/api/v2/song/by-isrc/{urllib.parse.quote(isrc)}", app_id, api_key)
    items = (data or {}).get("items") or []
    return items[0].get("uuid") if items else None


def _latest_charts(uuid: str, app_id: str, api_key: str) -> list[dict]:
    """최신 차트 랭킹 → [{chart_name, peak_rank, platform, region}]."""
    data = _get(f"/api/v2/song/{uuid}/charts/ranking/latest", app_id, api_key)
    out = []
    for it in (data or {}).get("items") or []:
        chart = it.get("chart") or {}
        out.append({
            "chart_name": chart.get("name"),
            "peak_rank": it.get("position"),
            "platform": chart.get("platform"),
            "region": chart.get("countryCode") or chart.get("country"),
        })
    return [c for c in out if c.get("chart_name")]


def _audience(uuid: str, platform: str, app_id: str, api_key: str) -> dict | None:
    """플랫폼 오디언스 최신 스냅샷 → {platform, metric, value, as_of}. 부재 시 None."""
    data = _get(f"/api/v2/song/{uuid}/audience/{platform}", app_id, api_key)
    items = (data or {}).get("items") or []
    if not items:
        return None
    latest = items[-1]   # 시계열 끝 = 최신
    value = latest.get("value") or latest.get("audience") or latest.get("count")
    if value is None:
        return None
    return {"platform": platform, "metric": "audience", "value": value,
            "as_of": (latest.get("date") or "")[:10]}


def soundcharts_metrics(
    isrc: str | None, app_id: str | None, api_key: str | None,
) -> dict:
    """ISRC로 Soundcharts 집계 보강. {uuid, charts, audience}.

    키/ISRC 없으면 {} (graceful). charts=Chart 노드용, audience=SocialMetric 노드용.
    """
    if not (isrc and app_id and api_key):
        return {}
    uuid = _song_uuid(isrc, app_id, api_key)
    if not uuid:
        logger.info("soundcharts: song 미해소 (ISRC %s)", isrc)
        return {}
    charts = _latest_charts(uuid, app_id, api_key)
    audience = []
    for plat in _AUDIENCE_PLATFORMS:
        a = _audience(uuid, plat, app_id, api_key)
        if a:
            audience.append(a)
    out = {"uuid": uuid, "charts": charts, "audience": audience}
    logger.info("soundcharts %s: charts=%d audience=%d", uuid, len(charts), len(audience))
    return out
