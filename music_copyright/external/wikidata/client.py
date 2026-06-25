"""Wikidata 커넥터 — 수상·노미네이트·레이블·창작자 정형 보강 (무료·무키).

웹검색이 못 주는 *정형* 영향력/권리 신호를 Wikidata에서 끌어온다:
    - 수상(P166)·노미네이트(P1411)         → Award 노드 (영향력)
    - 레이블/제작사(P264, P272)             → Rightsholder 노드 (지속성·권리)
    - 작곡(P86)·작사(P676)·프로듀서(P162)   → Artist 노드 보강 (창작 기여)

⚠️ SPARQL(query.wikidata.org)이 아니라 **MediaWiki API**(www.wikidata.org/w/api.php)를
쓴다 — WDQS는 간헐적 outage(429 1req/min)가 잦아 안정성이 떨어진다. API는
haswbstatement 검색 + wbgetentities claim 조회로 같은 결과를 더 안정적으로 준다.

식별 전략(가장 강한 것부터): MB work/recording MBID(P435/P4404) → ISWC(P1827) →
ISRC(P1243) → 제목 검색(wbsearchentities)+연주자 라벨 대조. 전부 best-effort —
부재/실패는 빈 결과로 흡수. 차트 순위·인증은 Wikidata가 부실해 여기서 안 한다
(Soundcharts 등 전용 집계가 담당).
"""

from __future__ import annotations

import logging
import urllib.parse

from music_copyright.external._http import get_json

logger = logging.getLogger("music_copyright.external.wikidata")

_API = "https://www.wikidata.org/w/api.php"
# Wikidata는 식별 가능한 UA를 요구한다(없으면 차단). seeder가 MB UA를 넘긴다.
_DEFAULT_UA = "STO-Valuation/1.0 (music-copyright valuation)"

# claim 프로퍼티 → (출력 버킷, role/kind 라벨)
_AWARD_PROPS = {"P166": "수상", "P1411": "노미네이트"}
_RIGHTS_PROPS = {"P264": "레이블", "P272": "제작사"}
_CREATOR_PROPS = {"P86": "composer", "P676": "lyricist", "P162": "producer"}


def _api(params: dict, user_agent: str) -> dict | None:
    base = {"format": "json", **params}
    url = f"{_API}?{urllib.parse.urlencode(base)}"
    return get_json(url, {"User-Agent": user_agent})


# ───────────────────────────────────────────────────────────
# 1) 엔티티(QID) 식별 — haswbstatement 검색(식별자) → 제목 검색(폴백)
# ───────────────────────────────────────────────────────────
def _search_by_statement(prop: str, value: str, user_agent: str) -> str | None:
    """haswbstatement:<prop>=<value> 검색 → 첫 QID. 식별자 정합이라 단일 매칭 기대."""
    data = _api({"action": "query", "list": "search",
                 "srsearch": f"haswbstatement:{prop}={value}", "srlimit": 1}, user_agent)
    hits = (data or {}).get("query", {}).get("search", [])
    return hits[0].get("title") if hits else None


def _search_by_title(title: str, artist: str | None, user_agent: str) -> str | None:
    """제목 검색 → artist 정합 첫 QID(동명이곡 오염 차단).

    정합 판정(싼 것부터): (1) 검색결과 description/label에 artist 포함
    (예: "2020 single by BTS") → 추가 호출 없이 즉시. (2) 폴백: 연주자(P175) 라벨을
    다국어(ko/en)로 받아 대조 — Wikidata 라벨이 '방탄소년단'/'BTS' 등 언어별로 달라
    한 언어만 보면 놓친다.
    """
    if not title:
        return None
    data = _api({"action": "wbsearchentities", "search": title, "language": "en",
                 "type": "item", "limit": 10}, user_agent)
    results = (data or {}).get("search", [])
    if not results:
        return None
    if not artist:
        return results[0].get("id")
    a = artist.lower()
    # (1) description/label 직접 대조
    for c in results:
        blob = f"{c.get('label','')} {c.get('description','')}".lower()
        if a in blob:
            return c.get("id")
    # (2) 연주자(P175) 라벨 다국어 대조
    cands = [c.get("id") for c in results if c.get("id")]
    ents = _get_entities(cands, "claims", user_agent)
    perf_qids = {c: _claim_qids(ents.get(c, {}), ["P175"]) for c in cands}
    all_perf = {q for qs in perf_qids.values() for q in qs}
    labels = _labels_multi(list(all_perf), user_agent) if all_perf else {}
    for c in cands:
        if any(any(a in v.lower() for v in labels.get(q, [])) for q in perf_qids[c]):
            return c
    return None


def _resolve_qid(ident: dict, user_agent: str) -> str | None:
    for prop, key in (("P435", "mb_work_mbid"), ("P4404", "mb_recording_mbid"),
                      ("P1827", "iswc"), ("P1243", "isrc")):
        v = ident.get(key)
        if v:
            qid = _search_by_statement(prop, v, user_agent)
            if qid:
                logger.debug("wikidata QID %s via %s", qid, key)
                return qid
    return _search_by_title(ident.get("title"), ident.get("artist"), user_agent)


# ───────────────────────────────────────────────────────────
# 2) claim 조회 + 라벨 해소 (wbgetentities)
# ───────────────────────────────────────────────────────────
def _get_entities(qids: list[str], props: str, user_agent: str) -> dict:
    """wbgetentities(최대 50개) → {qid: entity}. props='claims'|'labels'."""
    out: dict = {}
    for i in range(0, len(qids), 50):
        chunk = qids[i:i + 50]
        data = _api({"action": "wbgetentities", "ids": "|".join(chunk),
                     "props": props, "languages": "ko|en"}, user_agent)
        out.update((data or {}).get("entities", {}))
    return out


def _labels(qids: list[str], user_agent: str) -> dict[str, str]:
    """QID 리스트 → {qid: 라벨(ko 우선 en)}. 빈 입력은 {}."""
    if not qids:
        return {}
    ents = _get_entities(qids, "labels", user_agent)
    out = {}
    for q, e in ents.items():
        labs = e.get("labels", {})
        v = (labs.get("ko") or labs.get("en") or {}).get("value")
        if v:
            out[q] = v
    return out


def _labels_multi(qids: list[str], user_agent: str) -> dict[str, list[str]]:
    """QID 리스트 → {qid: [ko라벨, en라벨...]}. 다국어 대조용(언어별 라벨 차이 흡수)."""
    if not qids:
        return {}
    ents = _get_entities(qids, "labels", user_agent)
    out: dict[str, list[str]] = {}
    for q, e in ents.items():
        vals = [v.get("value") for v in e.get("labels", {}).values() if v.get("value")]
        if vals:
            out[q] = vals
    return out


def _claim_qids(entity: dict, props: list[str]) -> list[str]:
    """entity.claims[props..]의 mainsnak target QID 리스트(중복 제거)."""
    out = []
    claims = entity.get("claims", {})
    for p in props:
        for st in claims.get(p, []):
            qid = (((st.get("mainsnak") or {}).get("datavalue") or {})
                   .get("value") or {}).get("id")
            if qid and qid not in out:
                out.append(qid)
    return out


def _claim_rows(entity: dict, props: dict[str, str]) -> list[dict]:
    """claims[props..] → [{qid, kind, year}]. P585(time) 한정자에서 연도 추출."""
    rows = []
    claims = entity.get("claims", {})
    for p, kind in props.items():
        for st in claims.get(p, []):
            qid = (((st.get("mainsnak") or {}).get("datavalue") or {})
                   .get("value") or {}).get("id")
            if not qid:
                continue
            year = None
            for q in (st.get("qualifiers", {}) or {}).get("P585", []):
                t = ((q.get("datavalue") or {}).get("value") or {}).get("time")
                if t:
                    year = t.lstrip("+")[:4]
            rows.append({"qid": qid, "kind": kind, "year": year})
    return rows


def wikidata_enrich(
    *,
    title: str | None,
    artist: str | None = None,
    iswc: str | None = None,
    isrc: str | None = None,
    mb_work_mbid: str | None = None,
    mb_recording_mbid: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """식별자/제목으로 Wikidata 보강. {qid, awards, rightsholders, creators}.

    QID 미해소면 빈 dict. 모든 하위 조회는 best-effort. claim 조회 1회 + 참조 QID
    라벨 배치 조회 1회로 호출을 최소화한다.
    """
    ua = user_agent or _DEFAULT_UA
    ident = {"title": title, "artist": artist, "iswc": iswc, "isrc": isrc,
             "mb_work_mbid": mb_work_mbid, "mb_recording_mbid": mb_recording_mbid}
    qid = _resolve_qid(ident, ua)
    if not qid:
        logger.info("wikidata: QID 미해소 (%s)", title)
        return {}

    entity = _get_entities([qid], "claims", ua).get(qid, {})
    award_rows = _claim_rows(entity, _AWARD_PROPS)
    rights_rows = _claim_rows(entity, _RIGHTS_PROPS)
    creator_rows = _claim_rows(entity, _CREATOR_PROPS)

    # 참조 QID 라벨을 한 번에 해소.
    ref = list({r["qid"] for r in (award_rows + rights_rows + creator_rows)})
    lab = _labels(ref, ua)

    awards = [{"award_name": lab[r["qid"]], "result": r["kind"], "year": r["year"],
               "qid": r["qid"]} for r in award_rows if lab.get(r["qid"])]
    rightsholders = [{"name": lab[r["qid"]], "kind": r["kind"], "qid": r["qid"]}
                     for r in rights_rows if lab.get(r["qid"])]
    creators = [{"name": lab[r["qid"]], "role": r["kind"], "qid": r["qid"]}
                for r in creator_rows if lab.get(r["qid"])]

    out = {"qid": qid, "awards": awards, "rightsholders": rightsholders,
           "creators": creators}
    logger.info("wikidata %s: awards=%d rights=%d creators=%d", qid,
                len(awards), len(rightsholders), len(creators))
    return out
