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
from real_estate.graphflow.policies import StaticPolicy
from real_estate.graphflow.traversal import asset_anchor_eid, walk
from real_estate.graphflow.validity import validity

logger = logging.getLogger("graphflow.composer")


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


def _doc_origins(driver: Driver, fact_eids: list[str]) -> dict[str, dict]:
    """Fact eid → 출처 WebDoc {url,title,press} (첫 문서)."""
    if not fact_eids:
        return {}
    with driver.session() as s:
        result = s.run(
            f"""
            UNWIND $eids AS eid
            MATCH (f) WHERE elementId(f) = eid
            OPTIONAL MATCH (f)-[:{E.EXTRACTED_FROM}]->(d:{L.WEB_DOC})
            WITH eid, collect(d)[0] AS doc
            RETURN eid, doc.url AS url, doc.title AS title, doc.press AS press
            """,
            eids=fact_eids,
        )
        return {r["eid"]: {"url": r["url"], "title": r["title"], "press": r["press"]}
                for r in result if r["url"]}


def _source_kind(label: str) -> str:
    if label == L.FACT:
        return "news_fact"
    if label in (L.WEB_DOC, L.USER_DOC):
        return "news_doc"
    return "graph_stat"          # SQLite 적재 구조 노드 (상권 통계/인증/거래 등)


def compose(
    driver: Driver,
    asset_id: str,
    axis: str,
    *,
    k: int = 12,
    max_hops: int = 4,
) -> list[Evidence]:
    """축 프로파일 walk → Evidence 목록 (E1..Ek). 결정적."""
    anchor = asset_anchor_eid(driver, asset_id)
    if anchor is None:
        raise ValueError(f"Asset 노드 없음: {asset_id}")

    paths = walk(driver, [anchor], StaticPolicy(axis), max_hops=max_hops, k=k)
    tail_eids = [p.tail.eid for p in paths]
    props_map = _fetch_props(driver, tail_eids)
    origins = _doc_origins(
        driver, [p.tail.eid for p in paths if p.tail.label == L.FACT])

    out: list[Evidence] = []
    for i, p in enumerate(paths, 1):
        t = p.tail
        props = props_map.get(t.eid, {})
        v = validity(props, label=t.label)
        out.append(Evidence(
            index=i, ref=f"{t.label}:{t.key}", eid=t.eid, label=t.label,
            text=t.card_text, path_str=p.text(), score=p.score,
            source_kind=_source_kind(t.label),
            origin=origins.get(t.eid, {}),
            published_at=props.get("published_at"),
            valid_until=props.get("valid_until"),
            confidence=props.get("confidence"),
            validity_status=v.status,
        ))
    logger.info("compose(%s, %s): %d evidence", asset_id, axis, len(out))
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
