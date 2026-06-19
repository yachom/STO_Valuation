"""Composer — 섹션별 증거 수집 (순수 Cypher+walk, LLM 없음. 설계 §6 #5).

축별 StaticPolicy walk로 EvidencePath를 모으고, 보고서 evidence 맵(§10.3)에
필요한 해석 정보(출처/날짜/confidence/유효성/발견경로)까지 채운 Evidence
레코드로 변환한다. 결정적 — 같은 그래프 스냅샷이면 같은 증거.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from neo4j import Driver

from real_estate.graphflow.graph.schema import EDGE_TYPES as E
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.navigator import _fetch_props
from real_estate.graphflow.policies import SemanticPolicy, StaticPolicy
from real_estate.graphflow.traversal import (
    EdgeStep,
    EvidencePath,
    asset_anchor_eid,
    semantic_sims,
    walk,
)
from real_estate.graphflow.validity import validity

logger = logging.getLogger("graphflow.composer")

# 축별 '의도 문장' — SemanticPolicy 목적 벡터의 원천. 14개 엣지 상수표가 못 보는
# '이 노드가 이 축의 평가 관심사와 가까운가'를 임베딩 유사도로 반영한다.
AXIS_INTENT = {
    "tenant": "임차 사업자 안정성, 공실률과 임대료 시장 환경, 업종 회전율과 폐업, "
              "앵커 테넌트와 입점업체, 임대 수요의 질",
    "esg": "건물 에너지효율 등급, 녹색건축 인증, 물리적 상태와 준공 연식, 친환경 성능",
    "commercial": "상권 건강도와 활성도, 유동인구와 추정매출 추이, 주력 업종 구성, "
                  "배후수요, 교통 접근성, 개발 호재",
}

# 뉴스 검색 전용 의도 — 구조(SQLite/공공API)가 *못 가진* 동적·전망 정보로 타겟팅.
# AXIS_INTENT(공실/임대/매출 — 구조 데이터와 중복 → Writer가 통계를 택해 뉴스 사장)
# 대신, 뉴스만 줄 수 있는 GAP(개발 호재·입주 기업·신상권 경쟁)을 정조준한다.
AXIS_NEWS_INTENT = {
    "tenant": "임차인 입주 기업 이전, 신규 상권 경쟁과 임차 수요 이동, 공실 장기화와 "
              "임대료 협상, 프랜차이즈 출점·폐업 동향",
    "esg": "친환경 건축 인증과 신축 동향, 노후 건물 리모델링, 에너지 효율 규제 변화",
    "commercial": "지역 개발 호재, MICE 복합단지·대형 오피스 신축, 대기업 연구단지 입주, "
                  "교통 신설, 배후수요 증가, 신상권 형성과 상권 이동",
}


@dataclass
class Evidence:
    """보고서 인용 단위 — walk 경로 1건의 해석 정보 (§10.3 evidence 맵 항목)."""
    index: int                      # 축 내 [E#]
    ref: str                        # "Fact:abc123" (label:key)
    eid: str
    label: str
    text: str                       # card_text (근거 원문)
    path_str: str                   # 발견 경로 (카드 체인)
    score: float
    source_kind: str                # news_fact | news_doc | graph_stat
    origin: dict = field(default_factory=dict)   # {url,title,press} 또는 {}
    published_at: str | None = None
    valid_until: str | None = None
    confidence: float | None = None
    validity_status: str = "VALID"
    collected_by: str | None = None      # 수집 출처 run — QA '신규 수집 근거' 라벨용

    def to_map_entry(self) -> dict:
        return {
            "text": self.text,
            "source_kind": self.source_kind,
            "origin": self.origin,
            "published_at": self.published_at,
            "confidence": self.confidence,
            "validity": {"status": self.validity_status, "valid_until": self.valid_until},
            "path": self.path_str,
        }


def _doc_origins(driver: Driver, doc_eids: list[str]) -> dict[str, dict]:
    """문서성 노드 eid → origin {url,title,press,provider}.

    Fact → EXTRACTED_FROM 한 WebDoc의 url(뉴스), WebDoc → 자기 url(직접 인용),
    UserDoc → provider(첨부 자료, url 없음). 출처 링크의 원천 — Streamlit이 클릭
    가능하게 렌더하려면 모든 문서성 증거가 origin을 가져야 한다.
    """
    if not doc_eids:
        return {}
    with driver.session() as s:
        result = s.run(
            f"""
            UNWIND $eids AS eid
            MATCH (n) WHERE elementId(n) = eid
            OPTIONAL MATCH (n)-[:{E.EXTRACTED_FROM}]->(d:{L.WEB_DOC})
            WITH eid, n, collect(d)[0] AS doc
            RETURN eid,
                   coalesce(doc.url, n.url) AS url,
                   coalesce(doc.title, n.title) AS title,
                   coalesce(doc.press, n.press) AS press,
                   n.provider AS provider
            """,
            eids=doc_eids,
        )
        out = {}
        for r in result:
            o = {k: r[k] for k in ("url", "title", "press", "provider") if r[k]}
            if o:
                out[r["eid"]] = o
        return out


# 구조(graph_stat) 노드의 출처 — URL은 없지만 어느 데이터 소스/포털에서 왔는지
# 표기해 Streamlit에서 출처 배지+포털 링크로 렌더할 수 있게 한다 (설계 §10.3 의도).
_SOURCE_ATTR: dict[str, tuple[str, str]] = {
    L.TURNOVER_STAT: ("LocalData 지방행정 인허가", "https://www.localdata.go.kr"),
    L.MARKET_CONTEXT: ("한국부동산원 부동산통계(R-ONE) 임대동향", "https://www.reb.or.kr/r-one"),
    L.COMMERCIAL_AREA: ("서울 열린데이터광장 상권분석", "https://data.seoul.go.kr"),
    L.INDUSTRY: ("서울 열린데이터광장 상권 추정매출", "https://data.seoul.go.kr"),
    L.CERTIFICATION: ("건축물 에너지효율/녹색건축 인증", "https://www.greentogether.go.kr"),
    L.BUILDING: ("건축HUB 건축물대장", "https://www.eais.go.kr"),
    L.TENANT: ("카카오맵 장소정보", "https://map.kakao.com"),
    L.COMPLEX: ("부동산 단지 정보(KB/공공)", ""),
    L.TRANSIT_STATION: ("대중교통 정류장 정보", ""),
    L.ADMIN_DONG: ("서울 열린데이터광장 인구통계", "https://data.seoul.go.kr"),
    L.LEGAL_DONG: ("행정표준코드 법정동", ""),
    L.DEVELOPMENT_PROJECT: ("정비사업 정보", ""),
    L.TENANCY_EVENT: ("국토교통부 실거래가(MOLIT)", "https://rt.molit.go.kr"),
    L.ASSET: ("평가 대상/인접 자산 (카카오·Juso 주소해소)", ""),
}


def _source_origin(label: str) -> dict:
    """구조 노드의 출처 origin {source, url}. 미등록 라벨은 빈 dict."""
    attr = _SOURCE_ATTR.get(label)
    if not attr:
        return {}
    src, url = attr
    return {"source": src, "url": url} if url else {"source": src}


def _source_kind(label: str) -> str:
    if label == L.FACT:
        return "news_fact"
    if label in (L.WEB_DOC, L.USER_DOC):
        return "news_doc"
    return "graph_stat"          # SQLite 적재 구조 노드 (상권 통계/인증/거래 등)


def paths_to_evidence(driver: Driver, paths) -> list[Evidence]:
    """walk/llm_walk 경로 → Evidence 목록 (E1..En). Composer·QA 공용."""
    tail_eids = [p.tail.eid for p in paths]
    props_map = _fetch_props(driver, tail_eids)
    # 문서성 노드(Fact/WebDoc/UserDoc)는 url origin, 구조 노드는 출처 라벨 origin
    doc_eids = [p.tail.eid for p in paths
                if p.tail.label in (L.FACT, L.WEB_DOC, L.USER_DOC)]
    origins = _doc_origins(driver, doc_eids)

    out: list[Evidence] = []
    for i, p in enumerate(paths, 1):
        t = p.tail
        props = props_map.get(t.eid, {})
        v = validity(props, label=t.label)
        origin = origins.get(t.eid) or _source_origin(t.label)
        out.append(Evidence(
            index=i, ref=f"{t.label}:{t.key}", eid=t.eid, label=t.label,
            text=t.card_text, path_str=p.text(), score=p.score,
            source_kind=_source_kind(t.label),
            origin=origin,
            published_at=props.get("published_at"),
            valid_until=props.get("valid_until"),
            confidence=props.get("confidence"),
            validity_status=v.status,
            collected_by=props.get("collected_by"),
        ))
    return out


_FACT_LABELS = {L.FACT, L.WEB_DOC, L.USER_DOC}


def _reserve_facts(paths: list, *, k: int, reserve: int) -> list:
    """상위 k를 선택하되 뉴스 Fact/WebDoc 전용 슬롯을 보장한다 (결정적).

    정형 통계가 풍부한 자산에선 1홉 구조노드(Tenant/Turnover/Market)가 2홉 뉴스
    Fact(ANCHORS×MENTIONS)를 점수로 눌러 EXPAND 수집물이 보고서에 0건 인용되는
    문제가 있다. top-k에 Fact가 reserve개 미만이면 차순위 Fact 경로를 끌어올려
    비-Fact 하위 경로와 교체한다. paths는 점수 내림차순 가정 — 같은 그래프면
    같은 선택(결정적).
    """
    top = paths[:k]
    if reserve <= 0:
        return top
    facts_in_top = [p for p in top if p.tail.label in _FACT_LABELS]
    if len(facts_in_top) >= reserve:
        return top
    need = reserve - len(facts_in_top)
    extra = [p for p in paths[k:] if p.tail.label in _FACT_LABELS][:need]
    if not extra:
        return top
    nonfacts = [p for p in top if p.tail.label not in _FACT_LABELS]
    keep = facts_in_top + nonfacts[:max(0, len(nonfacts) - len(extra))] + extra
    keep.sort(key=lambda p: (-p.score, p.tail.label, p.tail.key))
    return keep[:k]


def _scope_fact_paths(
    driver: Driver, asset_id: str, purpose_text: str, anchor_eid: str, *,
    n: int, exclude: set[str], scope_hops: int = 4,
) -> list:
    """자산 스코프 내 Fact/WebDoc을 목적 텍스트로 직접 벡터검색 → 상위 n EvidencePath.

    그래프 walk는 뉴스 Fact를 못 표면화한다(MENTIONS 저가중·다홉 → top-k 밖). PPR은
    hub(WebDoc)에 쏠려 curated 리프를 잃는다. 둘 다 우회 — Fact 임베딩을 목적으로
    직접 검색해 '관련 뉴스/사실'을 결정적으로 확보하고 스코프로 한정한다(타 자산 누출
    차단). purpose_text: 보고서=AXIS_NEWS_INTENT, QA=질문. 공용.
    """
    from real_estate.graphflow.graph.embedder import embed_one
    from real_estate.graphflow.graph.vector_index import search_similar
    from real_estate.graphflow.traversal import _load_nodes, _scope_eids

    qv = embed_one(purpose_text)
    scope = _scope_eids(driver, asset_id, scope_hops)
    hits: dict[str, float] = {}
    for label in (L.FACT, L.WEB_DOC):
        try:
            for h in search_similar(driver, label, qv, k=n * 5):
                if h["eid"] in scope and h["eid"] not in exclude:
                    hits[h["eid"]] = max(hits.get(h["eid"], 0.0), h["score"])
        except Exception as e:                       # 인덱스 부재/빈 라벨
            logger.debug("scope fact search skip %s: %s", label, e)
    top = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    if not top:
        return []
    refs = _load_nodes(driver, [anchor_eid] + [e for e, _ in top])
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
    asset_id: str,
    axis: str,
    *,
    k: int = 12,
    max_hops: int = 4,
    fact_reserve: int = 0,
    semantic: bool = True,
    alpha: float = 0.6,
    news_k: int = 4,
) -> list[Evidence]:
    """축별 증거 = 그래프 walk(구조) + Fact 직접 벡터검색(뉴스) 병합. 결정적.

    semantic: SemanticPolicy(엣지 prior × 축 의도 코사인, 방법1). False면 StaticPolicy.
    news_k:   축 의도로 스코프 Fact를 직접 벡터검색해 보장하는 뉴스 슬롯. walk·PPR이
              못 푸는 뉴스 도달성을 직접 검색으로 해결(2단계 PoC 결론).
    fact_reserve: walk 경로 내 Fact 보장 슬롯(레거시 폴백, 기본 0 — news_k가 대체).
    """
    anchor = asset_anchor_eid(driver, asset_id)
    if anchor is None:
        raise ValueError(f"Asset 노드 없음: {asset_id}")
    if semantic and axis in AXIS_INTENT:
        sims = semantic_sims(driver, asset_id, AXIS_INTENT[axis], scope_hops=max_hops)
        policy = SemanticPolicy(axis, sims, alpha=alpha)
    else:
        policy = StaticPolicy(axis)
    paths = walk(driver, [anchor], policy, max_hops=max_hops, k=k * 2)
    structured = _reserve_facts(paths, k=k, reserve=fact_reserve) if fact_reserve else paths[:k]

    if news_k > 0 and axis in AXIS_INTENT:
        seen = {p.tail.eid for p in structured}
        news = _scope_fact_paths(
            driver, asset_id, AXIS_NEWS_INTENT.get(axis, AXIS_INTENT[axis]), anchor,
            n=news_k, exclude=seen, scope_hops=max_hops)
        if news:                                  # 뉴스 슬롯 확보 (구조 하위 교체)
            structured = structured[:k - len(news)] + news

    out = paths_to_evidence(driver, structured)
    n_fact = sum(1 for e in out if e.source_kind in ("news_fact", "news_doc"))
    logger.info("compose(%s, %s, %s): %d evidence (news=%d)", asset_id, axis,
                policy.name, len(out), n_fact)
    return out


def format_evidence(evidence: list[Evidence]) -> str:
    """Writer 프롬프트용 [E#] 블록 직렬화."""
    if not evidence:
        return "(증거 없음)"
    parts = []
    for ev in evidence:
        meta = [f"score={ev.score:.2f}", f"종류={ev.source_kind}"]
        if ev.published_at:
            meta.append(f"시점={ev.published_at[:10]}")
        if ev.confidence is not None:
            meta.append(f"confidence={ev.confidence}")
        if ev.validity_status != "VALID":
            meta.append(f"유효성={ev.validity_status}")
        if ev.origin.get("press"):
            meta.append(f"출처={ev.origin['press']}")
        body = "\n".join("   " + ln for ln in ev.path_str.splitlines())
        parts.append(f"[E{ev.index}] ({', '.join(meta)})\n{body}")
    return "\n\n".join(parts)
