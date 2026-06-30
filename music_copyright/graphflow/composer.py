"""Composer (음악저작권) — 축별 증거 수집 (recall 회수 → LLM 재랭킹 선별).

2단계:
    1) 회수(recall): Work 앵커에서 SemanticPolicy 가중 walk(멀티홉) + 축 의도 벡터검색으로
       후보 EvidencePath를 넓게 모은다. 구조적 안전 필터(타 작품·이전 보고서·미해소
       Entity 제외)만 적용한다.
    2) 선별(precision): 후보를 LLM 재랭킹(rerank.py)에 넘겨 축 관련성·대상 일치·사실성·
       출처 품질·중복을 판단해 상위 k를 고른다. 과거의 키워드 그룹·출처 감점·맥락 감점
       룰 더미를 대체한다(규칙 누적 회피).

LLM 재랭킹이라 결정적이지 않다(LLM 실패 시 벡터/walk 원점수 순으로 폴백 — 산출 안정성).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import unquote

from neo4j import Driver

from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.policies import SemanticPolicy, StaticPolicy
from music_copyright.graphflow.traversal import (
    EdgeStep,
    EvidencePath,
    corroboration_scores,
    load_nodes,
    scope_eids,
    semantic_sims,
    walk,
    work_anchor_eid,
)

logger = logging.getLogger("music_copyright.composer")

# 축 의도(semantic 검색 + 웹 Fact 타겟)의 단일 출처는 axes.BASE_AXES다. 동적 축은
# compose에 intent/news_intent를 직접 넘긴다(아래). 여기선 기본 3축 폴백 룩업만 둔다.
from music_copyright.graphflow.axes import BASE_AXES  # noqa: E402

_BASE_INTENT = {a.key: a.intent for a in BASE_AXES}
_BASE_NEWS = {a.key: a.news for a in BASE_AXES}

# 이전 실행의 보고서 산출 서브그래프 — 새 보고서의 외부 증거가 아니라 후보에서 제외.
_REPORT_OUTPUT_LABELS = {L.REPORT, L.REPORT_SECTION, L.CLAIM}


@dataclass
class Evidence:
    """보고서 인용 단위 — walk 경로 1건의 해석 정보."""
    index: int                      # 축 내 [E#]
    ref: str                        # "Platform:deezer" (label:key)
    eid: str
    label: str
    text: str                       # card_text (근거 원문)
    path_str: str                   # 발견 경로 (카드 체인)
    score: float
    source_kind: str                # graph_stat | news_fact | news_doc
    origin: dict = field(default_factory=dict)

    def to_map_entry(self) -> dict:
        return {
            "text": self.text,
            "source_kind": self.source_kind,
            "origin": self.origin,
            "path": self.path_str,
        }


# 구조 노드의 출처 표기 (URL 없는 노드도 어느 소스인지 배지로 렌더 가능하게).
_SOURCE_ATTR: dict[str, tuple[str, str]] = {
    L.WORK: ("작품 식별 (MusicBrainz / ISWC)", "https://musicbrainz.org"),
    L.RELEASE: ("발매물 맥락", ""),
    L.ARTIST: ("아티스트·창작자 (MusicBrainz)", "https://musicbrainz.org"),
    L.RIGHTSHOLDER: ("권리자", ""),
    L.AGENCY: ("소속사·매니지먼트", ""),
    L.GENRE: ("장르·스타일", ""),
    L.PLATFORM: ("유통 플랫폼 지표 (Spotify/Deezer/Last.fm)", ""),
    L.CHART: ("차트 기록", ""),
    L.AWARD: ("수상 기록", ""),
    L.MEDIA_EVENT: ("방송·공연·행사 노출", ""),
    L.VIRAL_TREND: ("챌린지·밈·해시태그 확산", ""),
    L.FANDOM: ("팬덤 공동체", ""),
    L.SOCIAL_THEME: ("사회적 의제 (가사·메시지 분석)", ""),
    L.CAUSE: ("사회운동·공익 연계", ""),
    L.PUBLIC_USE: ("공익·공적 활용 사례", ""),
}


def _source_kind(label: str) -> str:
    if label == L.FACT:
        return "news_fact"
    if label in (L.WEB_DOC,):
        return "news_doc"
    return "graph_stat"


def _source_origin(label: str) -> dict:
    attr = _SOURCE_ATTR.get(label)
    if not attr:
        return {}
    src, url = attr
    return {"source": src, "url": url} if url else {"source": src}


def paths_to_evidence(paths) -> list[Evidence]:
    """walk 경로 → Evidence 목록 (E1..En)."""
    out: list[Evidence] = []
    for i, p in enumerate(paths, 1):
        t = p.tail
        out.append(Evidence(
            index=i, ref=f"{t.label}:{t.key}", eid=t.eid, label=t.label,
            text=t.card_text, path_str=p.text(), score=p.score,
            source_kind=_source_kind(t.label), origin=_source_origin(t.label),
        ))
    return out


def _scope_fact_candidates(
    driver: Driver, work_id: str, purpose_text: str, anchor_eid: str, *,
    n: int, exclude: set[str], scope_hops: int = 4,
) -> list:
    """Work 스코프 내 Fact/WebDoc을 축 의도로 벡터검색 → 상위 n EvidencePath(원점수).

    그래프 walk가 못 표면화하는 저가중·다홉 Fact를 recall로 끌어오기 위한 후보 공급원.
    품질·관련성·대상일치 판단은 여기서 키워드 멀티플라이어로 하지 않고 LLM 재랭킹(rerank.py)
    에 맡긴다 — 여기선 scope 한정(타 작품 누출 차단)과 벡터 유사도 정렬만 한다.
    """
    from infra.graph.embedder import embed_one
    from music_copyright.graphflow.graph.vector_index import search_similar

    qv = embed_one(purpose_text)
    scope = scope_eids(driver, work_id, scope_hops)
    hits: dict[str, float] = {}
    for label in (L.FACT, L.WEB_DOC):
        try:
            for h in search_similar(driver, label, qv, k=n * 3):
                if h["eid"] in scope and h["eid"] not in exclude:
                    hits[h["eid"]] = max(hits.get(h["eid"], 0.0), h["score"])
        except Exception as e:                       # 인덱스 부재/빈 라벨
            logger.debug("scope fact candidate skip %s: %s", label, e)
    top = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    if not top:
        return []
    refs = load_nodes(driver, [anchor_eid] + [e for e, _ in top])
    anchor_ref = refs.get(anchor_eid)
    out = []
    for eid, score in top:
        nr = refs.get(eid)
        if nr is None or anchor_ref is None:
            continue
        # 표시용 경로 — 벡터 관련성으로 찾았음을 명시(그래프 경로 아님)
        out.append(EvidencePath([anchor_ref, nr], [EdgeStep("RELEVANT", "out", score)], score))
    return out


def compose(
    driver: Driver,
    work_id: str,
    axis: str,
    *,
    k: int | None = None,
    max_hops: int | None = None,
    semantic: bool | None = None,
    alpha: float | None = None,
    corroboration: bool | None = None,
    news_k: int | None = None,
    intent: str | None = None,
    news_intent: str | None = None,
    profile: str | None = None,
) -> list[Evidence]:
    """축별 증거 = 그래프 walk(SemanticPolicy) + Fact 벡터검색으로 후보 회수 → LLM 재랭킹 선별.

    기본값은 중앙 설정(config music_graphflow `retrieval`)에서 온다. 명시 인자가 우선.

    semantic: SemanticPolicy(엣지 prior × 축 의도 코사인). False면 StaticPolicy(레거시).
    intent / news_intent / profile: 동적 축용 — 안 주면 기본 3축 룩업(axis 키)으로 폴백.
              동적 축은 SemanticPolicy가 의도 코사인으로 관련 노드를 부상시키므로 수동
              가중치 표가 없어도 된다(profile 미등록 → StaticPolicy가 균형 prior로 폴백).
    news_k:   축 의도로 스코프 Fact를 직접 벡터검색해 보장하는 슬롯.
    """
    from music_copyright.graphflow.config import get_config
    r = get_config()["retrieval"]
    k = r["evidence_k"] if k is None else k
    max_hops = r["max_hops"] if max_hops is None else max_hops
    semantic = r.get("semantic", False) if semantic is None else semantic
    alpha = r.get("alpha", 0.6) if alpha is None else alpha
    corroboration = r.get("corroboration", False) if corroboration is None else corroboration
    news_k = r.get("news_k", 0) if news_k is None else news_k
    beta = r.get("corrob_beta", 0.35)

    # 동적 축이면 호출부가 intent를 넘긴다; 기본 3축은 키로 룩업.
    intent = intent or _BASE_INTENT.get(axis)
    news_intent = news_intent or _BASE_NEWS.get(axis) or intent
    profile = profile or axis

    anchor = work_anchor_eid(driver, work_id)
    if anchor is None:
        raise ValueError(f"Work 노드 없음(시드 선행 필요): {work_id}")

    if semantic and intent:
        sims = semantic_sims(driver, work_id, intent, scope_hops=max_hops)
        corrob = (corroboration_scores(driver, work_id, scope_hops=max(2, max_hops - 1))
                  if corroboration else None)
        policy = SemanticPolicy(profile, sims, alpha=alpha, corrob_by_eid=corrob, beta=beta)
    else:
        policy = StaticPolicy(profile)

    # ── 1) 후보 회수 (recall 지향) — 그래프 walk + 벡터검색을 넓게 모은다 ──
    paths = walk(driver, [anchor], policy, max_hops=max_hops, k=k * 3)
    # 구조적 안전 필터만 (provenance — 키워드 점수가 아니다):
    #  · 앵커 외 Work = 타 작품 누출  · Report/Claim = 이전 보고서 산출물
    #  · 미해소 Entity = 저가치 placeholder(승격된 건 typed 라벨이라 통과)
    paths = [p for p in paths
             if not (p.tail.label == L.WORK and p.tail.key != work_id)
             and p.tail.label not in _REPORT_OUTPUT_LABELS
             and p.tail.label not in {L.ENTITY, L.QUERY}]
    from music_copyright.graphflow.graph.queries import work_card_text
    work_context = work_card_text(driver, work_id)

    # 벡터 Fact/WebDoc 후보 (scope 한정, 원점수 — 멀티플라이어/키워드 감점 없음).
    # 작품 카드를 의도에 더해 이 작품에 고정(스코프가 hub-isolated라 타 작품은 안 샌다).
    if news_intent:
        seen = {p.tail.eid for p in paths}
        news_purpose = f"{work_context}\n{news_intent}"
        paths += _scope_fact_candidates(
            driver, work_id, news_purpose, anchor,
            n=max(news_k, k) * 2, exclude=seen, scope_hops=max_hops)

    candidates = _dedup_paths(paths)
    cand_evidence = paths_to_evidence(candidates)
    _enrich_origins(driver, cand_evidence)         # LLM이 출처를 보고 품질 판단하게

    # ── 2) 선별 (precision) — 키워드 룰 더미 대신 LLM이 축 관련성·대상일치·품질·중복 판단 ──
    from music_copyright.graphflow.rerank import rerank_evidence
    axis_intent = "\n".join(x for x in (intent, news_intent) if x)
    out = rerank_evidence(work_context, axis, axis_intent, cand_evidence, k=k)
    logger.info("compose(%s, %s, %s): %d evidence (rerank %d→%d)",
                work_id, axis, policy.name, len(out), len(cand_evidence), len(out))
    return out


def _dedup_paths(paths: list) -> list:
    """tail eid 기준 중복 제거(첫 등장 유지 — walk 점수순 우선)."""
    seen: set[str] = set()
    out = []
    for p in paths:
        if p.tail.eid in seen:
            continue
        seen.add(p.tail.eid)
        out.append(p)
    return out


def _enrich_origins(driver: Driver, evidence: list[Evidence]) -> None:
    """Fact/WebDoc/SocialMetric evidence에 실제 출처 URL을 붙인다."""
    if not evidence:
        return
    eids = [ev.eid for ev in evidence]
    with driver.session() as s:
        rows = s.run(
            f"""
            UNWIND $eids AS eid
            MATCH (n) WHERE elementId(n)=eid
            OPTIONAL MATCH (n)-[:{E.EXTRACTED_FROM}]->(d:{L.WEB_DOC})
            RETURN eid, labels(n)[0] AS label,
                   n.source AS n_source, n.url AS n_url, n.platform AS platform,
                   n.video_title AS video_title, n.channel AS channel,
                   d.source AS d_source, d.url AS d_url, d.title AS d_title,
                   d.channel AS d_channel
            """,
            eids=eids,
        ).data()
    by_eid = {r["eid"]: r for r in rows}
    for ev in evidence:
        r = by_eid.get(ev.eid)
        if not r:
            continue
        if ev.label == L.FACT:
            src = r.get("d_source") or r.get("n_source")
            url = r.get("d_url") or r.get("n_url")
            if src or url:
                ev.origin = {k: v for k, v in {
                    "source": src or "웹 수집 Fact",
                    "url": url,
                    "title": r.get("d_title"),
                }.items() if v}
            channel = r.get("d_channel") or "web"
            ev.source_kind = f"{channel}_fact"
        elif ev.label == L.WEB_DOC:
            if r.get("n_source") or r.get("n_url"):
                ev.origin = {k: v for k, v in {
                    "source": r.get("n_source") or "웹 문서",
                    "url": r.get("n_url"),
                }.items() if v}
            channel = r.get("d_channel") or "web"
            ev.source_kind = f"{channel}_doc"
        elif ev.label == L.SOCIAL_METRIC:
            if r.get("n_source") or r.get("n_url") or r.get("platform"):
                ev.origin = {k: v for k, v in {
                    "source": r.get("n_source") or r.get("platform"),
                    "url": r.get("n_url"),
                    "video_title": r.get("video_title"),
                    "channel": r.get("channel"),
                }.items() if v}


def format_evidence(evidence: list[Evidence]) -> str:
    """Writer 프롬프트용 [E#] 블록 직렬화."""
    if not evidence:
        return "(증거 없음)"
    parts = []
    for ev in evidence:
        meta = [f"score={ev.score:.2f}", f"종류={ev.source_kind}"]
        if ev.origin.get("source"):
            meta.append(f"출처={ev.origin['source']}")
        body = "\n".join("   " + ln for ln in ev.path_str.splitlines())
        parts.append(f"[E{ev.index}] ({', '.join(meta)})\n{body}")
    return "\n\n".join(parts)
