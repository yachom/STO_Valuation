"""Composer (음악저작권) — 축별 증거 수집 (StaticPolicy walk). real_estate 미러.

v1 스켈레톤: Work 앵커에서 축별 StaticPolicy 가중 walk로 EvidencePath를 모아
Evidence 레코드로 변환한다. 결정적 — 같은 그래프 스냅샷이면 같은 증거.

⚠️ TODO(후속): real_estate의 semantic(축 의도 임베딩)·뉴스 Fact 직접 벡터검색·
corroboration은 EXPAND(웹검색 Fact 적재) + 임베딩이 갖춰진 뒤 동일 형태로 붙인다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from neo4j import Driver

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

# Fact 슬롯 보장/식별용 라벨 (웹검색 수집물).
_FACT_LABELS = {L.FACT, L.WEB_DOC}


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
    L.ARTIST: ("아티스트·창작자 (MusicBrainz)", "https://musicbrainz.org"),
    L.RIGHTSHOLDER: ("권리자", ""),
    L.PLATFORM: ("유통 플랫폼 지표 (Spotify/Deezer/Last.fm)", ""),
    L.CHART: ("차트 기록", ""),
    L.AWARD: ("수상 기록", ""),
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


def _scope_fact_paths(
    driver: Driver, work_id: str, purpose_text: str, anchor_eid: str, *,
    n: int, exclude: set[str], scope_hops: int = 4,
) -> list:
    """Work 스코프 내 Fact/WebDoc을 축 의도로 직접 벡터검색 → 상위 n EvidencePath.

    그래프 walk는 저가중·다홉 Fact를 못 표면화한다. Fact 임베딩을 축 의도로 직접 검색해
    '관련 사실/뉴스'를 결정적으로 확보하고 스코프로 한정한다(타 작품 누출 차단).
    """
    from infra.graph.embedder import embed_one
    from music_copyright.graphflow.graph.vector_index import search_similar

    qv = embed_one(purpose_text)
    scope = scope_eids(driver, work_id, scope_hops)
    fact_hits: dict[str, float] = {}
    doc_hits: dict[str, float] = {}
    for label, bucket in ((L.FACT, fact_hits), (L.WEB_DOC, doc_hits)):
        try:
            for h in search_similar(driver, label, qv, k=n * 5):
                if h["eid"] in scope and h["eid"] not in exclude:
                    bucket[h["eid"]] = max(bucket.get(h["eid"], 0.0), h["score"])
        except Exception as e:                       # 인덱스 부재/빈 라벨
            logger.debug("scope fact search skip %s: %s", label, e)
    # Fact(원자적 claim) 우선 — 헤드라인(WebDoc 제목)보다 보고서 grounding에 직접 유용.
    # news 슬롯의 ≥2/3을 Fact에 배정하고, 부족분만 WebDoc으로 채운다.
    fact_top = sorted(fact_hits.items(), key=lambda kv: (-kv[1], kv[0]))
    doc_top = sorted(doc_hits.items(), key=lambda kv: (-kv[1], kv[0]))
    n_fact = min(len(fact_top), max(1, (n * 2 + 2) // 3))
    top = fact_top[:n_fact] + doc_top[:max(0, n - n_fact)]
    top = sorted(top, key=lambda kv: (-kv[1], kv[0]))[:n]
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
    """축별 증거 = 그래프 walk(SemanticPolicy) + Fact 직접 벡터검색 병합. 결정적.

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

    paths = walk(driver, [anchor], policy, max_hops=max_hops, k=k * 2)
    # 타 작품 Work 노드 누출 차단 — 5개 작품이 공유 Platform/Artist 허브로 연결돼
    # walk가 다른 곡 Work를 이 곡 근거로 끌어온다(교차오염). 앵커 외 Work는 evidence 제외.
    paths = [p for p in paths
             if not (p.tail.label == L.WORK and p.tail.key != work_id)]
    structured = paths[:k]

    if news_k > 0 and news_intent:
        from music_copyright.graphflow.graph.queries import work_card_text
        seen = {p.tail.eid for p in structured}
        # 작품-앵커링: 축 의도에 작품 카드(제목·아티스트)를 더해 벡터검색을 이 작품에
        # 고정한다 — 안 그러면 같은 축('수상')이면 다른 곡의 Fact도 매칭돼 섞인다
        # (웹검색 노이즈: Dynamite 검색이 가져온 문서에 타 아티스트가 섞인 경우).
        news_purpose = f"{work_card_text(driver, work_id)}\n{news_intent}"
        news = _scope_fact_paths(
            driver, work_id, news_purpose, anchor,
            n=news_k, exclude=seen, scope_hops=max_hops)
        if news:                                  # 뉴스 슬롯 확보 (구조 하위 교체)
            structured = structured[:k - len(news)] + news

    out = paths_to_evidence(structured)
    logger.info("compose(%s, %s, %s): %d evidence", work_id, axis, policy.name, len(out))
    return out


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
