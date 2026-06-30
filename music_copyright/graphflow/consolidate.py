"""CONSOLIDATE 단계 — 결정적 구조 보강 (현재: Rightsholder→Agency, LLM 분류).

2026-06-30: 하드코딩 토큰 사전을 전부 제거했다. 팬덤·차트·수상 등은 검색-인식(하베스터
guess_label) + agentic_graph(LLM 제안 + validator)로 일임한다.

여기 남은 결정적 작업은 **시드된 정형 Rightsholder 중 소속사(기획사)를 Artist에 MANAGED_BY로
잇는 구조 보강**이다. 이전엔 `_AGENCY_TOKENS`(hybe/jyp/sm…)로 소속사 vs 음반사를 갈랐는데,
중소 소속사마다 토큰을 다는 수작업이라 **LLM 분류기(`_AgencyClassifier`)로 대체**했다 — 회사명을
외운 목록이 아니라 '이 회사가 아티스트를 매니지먼트하는 기획사인가'를 이름 관례·일반 지식으로
판단한다(새 소속사도 토큰 없이 인식). 입력이 정형 Rightsholder(HELD_BY)라 자동커밋해도 안전하고,
LLM이 애매하다고 본 권리자는 Rightsholder로 남는다(필요 시 사람이 검토). web에서 인식되는
Agency는 agentic_graph가 MANAGED_BY(review 티어)로 별도 처리한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from neo4j import Driver
from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import (
    BaseAgent,
    music_system_prompt,
    normalize_text,
    short_hash,
)
from music_copyright.graphflow.graph.cards import build_card
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.graph.schema import apply_constraints
from music_copyright.graphflow.prompts import get_prompt

logger = logging.getLogger("music_copyright.consolidate")


@dataclass(frozen=True)
class _ArtistCtx:
    key: str
    name: str
    role: str | None = None


@dataclass(frozen=True)
class _WorkCtx:
    work_id: str
    title: str
    artist: _ArtistCtx | None


class _AgencyClassification(BaseModel):
    agency_names: list[str] = Field(
        default_factory=list,
        description="입력 권리자 중 이 아티스트의 소속사·매니지먼트(기획사)인 이름만. "
                    "음반사·유통사·퍼블리셔 제외. 입력에 있는 이름 그대로.")


class _AgencyClassifier(BaseAgent):
    """정형 Rightsholder를 소속사 vs 음반사로 분류 — 토큰 사전 대체(LLM 판단)."""

    name = "music_agency_classifier"

    def classify(self, artist_name: str, rightsholder_names: list[str]) -> set[str]:
        names = [n for n in rightsholder_names if n and n.strip()]
        if not names:
            return set()
        user = (f"[아티스트] {artist_name}\n[권리자 목록]\n"
                + "\n".join(f"- {n}" for n in names))
        try:
            out: _AgencyClassification = self.complete(
                system=music_system_prompt(get_prompt("music_agency_classifier.system")),
                user=user, schema=_AgencyClassification)
        except Exception as exc:  # noqa: BLE001 — best-effort, 시드를 막지 않음
            logger.warning("agency classify 실패: %s", exc)
            return set()
        return {normalize_text(n) for n in out.agency_names}


def consolidate_entities(
    driver: Driver,
    work_id: str,
    *,
    mode: str = "rules_first",
    min_conf: float = 0.6,
    scope_hops: int = 4,
) -> dict[str, Any]:
    """결정적 구조 보강 — 현재는 Rightsholder→Agency(MANAGED_BY)뿐. 멱등 MERGE.

    팬덤·차트·수상 등 개방형 엔티티 승격은 검색(harvester guess_label) + agentic_graph로
    일임한다(이 단계는 하드코딩 이름 사전을 쓰지 않는다)."""
    _ = mode, min_conf, scope_hops  # 호출부 호환 시그니처 유지
    apply_constraints(driver)
    ctx = _load_work_context(driver, work_id)
    if ctx is None:
        return {"work_id": work_id, "skipped": "work_not_found"}
    agencies = _seed_agencies_from_rightsholders(driver, ctx)
    stats = {"work_id": work_id, "agencies_seeded": agencies}
    logger.info("consolidate(%s): %s", work_id, stats)
    return stats


def _load_work_context(driver: Driver, work_id: str) -> _WorkCtx | None:
    with driver.session() as s:
        rec = s.run(
            f"""
            MATCH (w:{L.WORK} {{{KEY_PROP[L.WORK]}: $wid}})
            OPTIONAL MATCH (w)-[r:{E.BY_ARTIST}]->(a:{L.ARTIST})
            RETURN w.title AS title,
                   collect({{
                     key: a.{KEY_PROP[L.ARTIST]},
                     name: coalesce(a.name, a.artist_name),
                     role: coalesce(r.role, a.role)
                   }}) AS artists
            """,
            wid=work_id,
        ).single()
    if rec is None:
        return None
    artists = [a for a in (rec["artists"] or []) if a.get("key") and a.get("name")]
    picked = None
    for a in artists:
        role = (a.get("role") or "").lower()
        if "performer" in role or "artist" in role or "main" in role:
            picked = a
            break
    picked = picked or (artists[0] if artists else None)
    artist = (_ArtistCtx(str(picked["key"]), str(picked["name"]), picked.get("role"))
              if picked else None)
    return _WorkCtx(work_id=work_id, title=rec["title"] or "", artist=artist)


def _seed_agencies_from_rightsholders(driver: Driver, ctx: _WorkCtx) -> int:
    """시드된 Rightsholder 중 소속사를 Artist-[:MANAGED_BY]->Agency로 보강(LLM 분류)."""
    if ctx.artist is None:
        return 0
    with driver.session() as s:
        rows = s.run(
            f"""
            MATCH (w:{L.WORK} {{{KEY_PROP[L.WORK]}: $wid}})
                  -[:{E.HELD_BY}]->(r:{L.RIGHTSHOLDER})
            RETURN r.{KEY_PROP[L.RIGHTSHOLDER]} AS rightsholder_id,
                   coalesce(r.name, r.rightsholder_name) AS name
            """,
            wid=ctx.work_id,
        ).data()
    named = [(r.get("rightsholder_id"), r.get("name") or "")
             for r in rows if (r.get("name") or "").strip()]
    if not named:
        return 0
    # LLM 분류(세션 밖) — 토큰 매칭이 아니라 '이 권리자가 소속사인가'를 판단.
    agency_norms = _AgencyClassifier().classify(ctx.artist.name, [n for _, n in named])
    if not agency_norms:
        return 0
    n = 0
    with driver.session() as s:
        for rid, name in named:
            if normalize_text(name) not in agency_norms:
                continue
            key = f"agency:{short_hash(normalize_text(name))}"
            _merge_node(s, L.AGENCY, key, {
                "name": name,
                "agency_name": name,
                "kind": "기획사·매니지먼트",
                "artist_name": ctx.artist.name,
                "source_rightsholder_id": rid,
                "classified_by": "music_agency_classifier",
            })
            _merge_edge(s, L.ARTIST, ctx.artist.key, E.MANAGED_BY, L.AGENCY, key)
            n += 1
    return n


def _merge_node(session, label: str, key: str, props: dict[str, Any]) -> None:
    key_prop = KEY_PROP[label]
    clean = {k: v for k, v in props.items() if v is not None and v != ""}
    card = build_card(label, {**clean, key_prop: key})
    session.run(
        f"MERGE (n:{label} {{{key_prop}: $key}}) SET n += $props, n.card_text = $card",
        key=key, props=clean, card=card,
    )


def _merge_edge(session, src_label, src_key, rel, dst_label, dst_key, props=None) -> None:
    sk, dk = KEY_PROP[src_label], KEY_PROP[dst_label]
    session.run(
        f"MATCH (s:{src_label} {{{sk}: $src}}) MATCH (d:{dst_label} {{{dk}: $dst}}) "
        f"MERGE (s)-[r:{rel}]->(d) SET r += $props",
        src=src_key, dst=dst_key, props=props or {},
    )
