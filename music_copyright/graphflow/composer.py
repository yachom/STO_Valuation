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
from music_copyright.graphflow.policies import StaticPolicy
from music_copyright.graphflow.traversal import walk, work_anchor_eid

logger = logging.getLogger("music_copyright.composer")


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


def compose(
    driver: Driver,
    work_id: str,
    axis: str,
    *,
    k: int | None = None,
    max_hops: int | None = None,
) -> list[Evidence]:
    """축별 증거 = Work 앵커에서 StaticPolicy 가중 walk. 결정적.

    기본값(k·max_hops)은 중앙 설정(config music_graphflow `retrieval`)에서 온다.
    """
    from music_copyright.graphflow.config import get_config
    r = get_config()["retrieval"]
    k = r["evidence_k"] if k is None else k
    max_hops = r["max_hops"] if max_hops is None else max_hops

    anchor = work_anchor_eid(driver, work_id)
    if anchor is None:
        raise ValueError(f"Work 노드 없음(시드 선행 필요): {work_id}")

    policy = StaticPolicy(axis)
    paths = walk(driver, [anchor], policy, max_hops=max_hops, k=k)
    out = paths_to_evidence(paths)
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
