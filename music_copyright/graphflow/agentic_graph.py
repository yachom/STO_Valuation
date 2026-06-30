"""Agentic graph proposal stage for music copyright reports.

This stage gives the LLM autonomy where it is useful: reading the current graph
state and proposing entity-resolution / social-signal updates. It does not let
the LLM write arbitrary Cypher. Proposals pass through deterministic validators
and only a small set of low-risk labels is auto-committed; everything else is
left as review metadata.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from infra.agent.tracing import emit
from neo4j import Driver
from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import (
    BaseAgent,
    music_system_prompt,
    normalize_text,
    short_hash,
)
from music_copyright.graphflow.coverage import axis_coverage, coverage_summary_text
from music_copyright.graphflow.graph.cards import build_card
from music_copyright.graphflow.graph.schema import EDGE_ENDPOINTS
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import KEY_PROP
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.prompts import get_prompt
from music_copyright.graphflow.traversal import scope_eids

logger = logging.getLogger("music_copyright.agentic_graph")

# LLM이 제안할 수 있는 관계(payload). LLM은 (엔티티 → 관계 → 노드)를 근거와 함께 제안하고,
# 코드는 스키마 합법(EDGE_ENDPOINTS) + provenance + work_anchored + 자동커밋 정책만 검증한다.
# COVERED_BY/SAMPLED_BY(dst=Artist/Work)는 네임스페이스 위험이 커 제외 — 시드 정형이 담당.
_PROPOSABLE_EDGES = {
    E.CHARTED_ON, E.WON, E.CERTIFIED, E.HAS_FANDOM, E.MANAGED_BY, E.ADDRESSES,
    E.SUPPORTED, E.USED_IN, E.FEATURED_IN, E.SPREAD_AS,
}
# 자동커밋 = 객관적·구별적 엣지(사용자 결정 2026-06-29). ① 성취(CHARTED_ON/WON/CERTIFIED):
# 근거 Fact가 작품 결과를 직접 명시. ② HAS_FANDOM: 팬덤은 하베스터가 guess_label로 검색-인식한
# 구별적 타입이고 performer Artist에 grounding과 함께 앵커되므로 자동커밋해도 안전 — 이로써
# consolidate의 하드코딩 팬덤 사전(ARMY/BLINK/…)을 검색기반 인식으로 대체한다. 반면 관계·해석성
# 엣지(MANAGED_BY/SUPPORTED/ADDRESSES/USED_IN/FEATURED_IN/SPREAD_AS)는 의미 과대귀속 위험(WHO를
# 소속사로·아티스트 활동을 곡 메시지로)이 커 validator만으론 못 거른다 → LLM 제안·rationale은
# 남기되 review_candidate로 사람 검토에 맡긴다. _AUTO_COMMIT_EDGES는 하드 실링(cfg는 이 안에서만
# 제한 가능, 확장 불가).
_AUTO_COMMIT_EDGES = {E.CHARTED_ON, E.WON, E.CERTIFIED, E.HAS_FANDOM}
# relationship 미지정 시 라벨로 기본 엣지 폴백(하위호환 — 기존 social/Fandom 경로 보존).
_DEFAULT_EDGE_FOR_LABEL = {
    L.FANDOM: E.HAS_FANDOM, L.AGENCY: E.MANAGED_BY, L.SOCIAL_THEME: E.ADDRESSES,
    L.CAUSE: E.SUPPORTED, L.PUBLIC_USE: E.USED_IN, L.CHART: E.CHARTED_ON,
    L.AWARD: E.WON, L.CERTIFICATION: E.CERTIFIED, L.MEDIA_EVENT: E.FEATURED_IN,
    L.VIRAL_TREND: E.SPREAD_AS,
}
_CANDIDATE_LABELS = {
    L.FANDOM, L.AGENCY, L.AWARD, L.CHART, L.MEDIA_EVENT, L.VIRAL_TREND,
    L.GENRE, L.RELEASE, L.PLATFORM, L.SOCIAL_THEME, L.CAUSE, L.PUBLIC_USE,
}
_GRAPH_OUTPUT_LABELS = {L.REPORT, L.REPORT_SECTION, L.CLAIM, L.QUERY}

# 라벨별 카테고리 토큰 리스트는 제거했다 — 무엇이 Fandom/SocialTheme/Cause/PublicUse인지의
# '판단'은 proposer LLM 몫이다(프롬프트가 각 라벨의 의미를 산문으로 정의). 코드는 점수/토큰
# 매칭을 하지 않는다. _GENERIC_STOP만 남긴다(노드로 만들 수 없는 일반명사 차단 — 안전장치).
_GENERIC_STOP = {
    "this song", "song", "track", "music", "artist", "팬", "곡", "노래", "아티스트",
    "가수", "무대", "영상", "조회수", "1위",
}


@dataclass(frozen=True)
class FactCtx:
    fact_id: str
    text: str
    source_tier: float | None = None
    url: str | None = None
    title: str | None = None
    source_excerpt: str | None = None

    def to_prompt(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "text": self.text[:400],
            "source_tier": self.source_tier,
            "url": self.url,
            "title": self.title[:120] if self.title else None,
            "source_excerpt": self.source_excerpt[:300] if self.source_excerpt else None,
        }


@dataclass(frozen=True)
class EntityCtx:
    entity_id: str
    name: str
    guess_label: str | None = None
    resolution_status: str | None = None
    candidate_label: str | None = None
    facts: tuple[FactCtx, ...] = field(default_factory=tuple)
    # 구조적 작품 앵커링: 이 Entity를 언급한 Fact가 평가 대상 Work를 MENTIONS하거나
    # 그 Fact의 WebDoc이 Work에 ABOUT인가. 곡명/아티스트명 문자열 매칭(_has_asset_context)
    # 보다 일반적이고 견고한 신호 — 기부·공익 Fact는 보통 곡명을 본문에 반복하지 않는다.
    work_anchored: bool = False
    collection_anchored: bool = False

    def to_prompt(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "guess_label": self.guess_label,
            "resolution_status": self.resolution_status,
            "candidate_label": self.candidate_label,
            # 평가 대상 Work에 그래프상 실제로 연결돼 있는가(이 Entity를 언급한 Fact가 Work를
            # 가리키거나 그 출처 문서가 Work 기사인가). 텍스트만으론 판단 못 하는 교차오염
            # 분별 신호 — LLM은 work_anchored=false면 promote하지 말고 reject/review로 둘 것.
            "work_anchored": self.work_anchored,
            "collection_anchored": self.collection_anchored,
            "facts": [f.to_prompt() for f in self.facts[:5]],
        }


@dataclass(frozen=True)
class WorkCtx:
    work_id: str
    title: str
    artist_key: str | None = None
    artist_name: str | None = None


@dataclass
class GraphState:
    work: WorkCtx
    work_card: str
    coverage_text: str
    entities: list[EntityCtx]
    scope_refs: dict[str, str]
    # (label, normalize_text(name)) → key : 승격 시 기존 시드/이전 승격 노드로 dedup MERGE.
    scope_name_index: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def by_entity_id(self) -> dict[str, EntityCtx]:
        return {e.entity_id: e for e in self.entities}


class GraphProposal(BaseModel):
    action: Literal["promote_entity", "same_as_candidate", "mark_review", "reject"] = Field(
        description="제안 유형")
    entity_id: str = Field(description="입력 목록의 Entity.entity_id")
    target_label: str | None = Field(default=None, description="목표 라벨")
    target_name: str | None = Field(default=None, description="새 노드/후보의 사람이 읽는 이름")
    existing_ref: str | None = Field(
        default=None, description="same_as_candidate일 때 기존 노드 ref, 예: Chart:billboard-hot-100")
    relationship: str | None = Field(
        default=None,
        description="promote_entity 시 평가 대상에 대한 관계(엣지) 종류. 예: CHARTED_ON, WON, "
                    "HAS_FANDOM, ADDRESSES. 미지정 시 라벨 기본값. 스키마상 합법한 관계만 커밋됨")
    anchor_ref: str | None = Field(
        default=None,
        description="관계를 걸 대상(src) 노드 ref. 예: Work:<id> 또는 Artist:<key>. 승격 엔티티가 "
                    "dst, 이 노드가 src. 미지정 시 기본 앵커(작품 관계→Work, 아티스트 관계→Artist)")
    evidence_fact_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, description="0~1")
    rationale: str = Field(default="", description="결정 근거 1문장")


class GraphProposalOut(BaseModel):
    proposals: list[GraphProposal] = Field(default_factory=list)


class GraphProposalAgent(BaseAgent):
    name = "music_graph_proposer"

    def propose(self, state: GraphState, *, max_proposals: int) -> list[GraphProposal]:
        payload = {
            "work": {
                "work_id": state.work.work_id,
                "title": state.work.title,
                "artist_name": state.work.artist_name,
            },
            "work_card": state.work_card,
            "coverage": state.coverage_text,
            "existing_scope_refs": state.scope_refs,
            "unresolved_entities": [e.to_prompt() for e in state.entities],
            # LLM이 (엔티티 → 관계 → 노드)를 판단하도록 허용 관계와 그 (src→dst) 스키마를 준다.
            "allowed_relationships": {
                rel: {"src": sorted(EDGE_ENDPOINTS[rel][0]),
                      "dst": sorted(EDGE_ENDPOINTS[rel][1])}
                for rel in sorted(_PROPOSABLE_EDGES)
            },
            "allowed_candidate_labels": sorted(_CANDIDATE_LABELS),
            "max_proposals": max_proposals,
        }
        out: GraphProposalOut = self.complete(
            system=music_system_prompt(get_prompt("music_graph_proposer.system")),
            user=json.dumps(payload, ensure_ascii=False, indent=2),
            schema=GraphProposalOut,
        )
        return out.proposals[:max_proposals]


def run_agentic_graph(driver: Driver, work_id: str, *, config: dict | None = None) -> dict[str, Any]:
    """Summarize graph state, ask for proposals, validate, and commit safe updates."""
    from music_copyright.graphflow.config import get_config

    cfg = config or get_config().get("agentic_graph", {})
    if not cfg.get("enabled", False):
        summary = {"enabled": False, "skipped": True}
        emit("agentic_graph", **summary)
        return summary

    state = summarize_graph_state(
        driver,
        work_id,
        scope_hops=int(cfg.get("scope_hops", 4)),
        max_entities=int(cfg.get("max_entities", 24)),
        facts_per_entity=int(cfg.get("facts_per_entity", 5)),
    )
    if not state.entities:
        summary = {"enabled": True, "skipped": True, "reason": "no_unresolved_entities"}
        emit("agentic_graph", **summary)
        return summary

    max_proposals = int(cfg.get("max_proposals", 12))
    try:
        proposals = GraphProposalAgent().propose(state, max_proposals=max_proposals)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agentic graph proposal failed: %s", exc)
        summary = {"enabled": True, "skipped": True, "reason": "agent_error",
                   "error": type(exc).__name__}
        emit("agentic_graph", **summary)
        return summary

    result = apply_graph_proposals(driver, state, proposals, cfg)
    emit("agentic_graph", **result)
    return result


def summarize_graph_state(
    driver: Driver,
    work_id: str,
    *,
    scope_hops: int = 4,
    max_entities: int = 24,
    facts_per_entity: int = 5,
) -> GraphState:
    work = _load_work_context(driver, work_id)
    if work is None:
        work = WorkCtx(work_id=work_id, title="")
    work_card = _work_card(driver, work_id)
    cov_text = coverage_summary_text(axis_coverage(driver, work_id))
    eids = scope_eids(driver, work_id, scope_hops)
    entities = _load_entities(driver, eids, work_id=work_id,
                             facts_per_entity=facts_per_entity)
    entities.sort(key=lambda e: (
        0 if e.resolution_status not in {"promoted", "agent_promoted"} else 1,
        -len(e.facts),
        e.name,
    ))
    refs = _load_scope_refs(driver, eids)
    name_index = _load_scope_name_index(driver, eids)
    return GraphState(
        work=work,
        work_card=work_card,
        coverage_text=cov_text,
        entities=entities[:max_entities],
        scope_refs=refs,
        scope_name_index=name_index,
    )


def apply_graph_proposals(
    driver: Driver,
    state: GraphState,
    proposals: list[GraphProposal],
    cfg: dict,
) -> dict[str, Any]:
    # 무엇을/어떤 관계로 승격할지 '판단'은 proposer LLM(평가 목적 + 수집 Fact + work_anchored
    # + 허용 관계 스키마 제공) 몫이고, 코드는 '점수'가 아니라 안전장치만 본다: 허용 엣지인가,
    # 스키마상 합법 관계인가(EDGE_ENDPOINTS), 근거 Fact가 실재+source_excerpt인가, 작품에
    # 구조적으로 앵커됐나. min_conf는 게이트가 아니다(rubric 없는 자가보고라 의미 약함).
    # 게이트 단위는 '라벨'이 아니라 '관계(엣지)'다 — cfg override는 코드 allowlist를 못 넘는다.
    auto_edges = {e for e in (cfg.get("auto_commit_edges") or _AUTO_COMMIT_EDGES)
                  if e in _AUTO_COMMIT_EDGES}

    stats: dict[str, Any] = {
        "enabled": True,
        "entities_seen": len(state.entities),
        "proposals": len(proposals),
        # committed = 실제 그래프 엔티티 해소 커밋(typed 승격 + same_as 후보연결)만 센다.
        # review/reject는 메타데이터만 쓰므로 review_marked/rejected로 따로 집계한다
        # (과거엔 전부 committed에 합산돼 '커밋=3'이 승격 0건을 가렸다).
        "committed": 0,
        "promoted": 0,
        "promoted_by_edge": {},
        "same_as_candidates": 0,
        "review_marked": 0,
        "rejected": 0,
        "skipped": 0,
        "skip_reasons": {},
    }
    by_entity = state.by_entity_id

    for proposal in proposals:
        entity = by_entity.get(proposal.entity_id) or _entity_by_surface(state, proposal.entity_id)
        if entity is None:
            _skip(stats, "unknown_entity")
            continue

        # LLM의 action 판단을 그대로 존중한다(제안자=판단자). 코드는 안전장치만 본다.
        if proposal.action == "promote_entity":
            label = proposal.target_label
            rel = _resolve_relationship(proposal, label)
            src_label, src_key = (
                _resolve_anchor(state, proposal, rel) if rel in EDGE_ENDPOINTS else (None, None))
            ok, reason = _validate_promotion(
                state, entity, proposal, label, rel, src_label, auto_edges)
            if not ok:                              # 안전장치 미충족 → 검토 후보로
                _mark_review(driver, entity, proposal, label or "Unknown")
                stats["review_marked"] += 1
                _skip(stats, f"promotion_guard:{reason}")
                continue
            _commit_promotion(driver, state, entity, proposal, label, rel, src_label, src_key)
            stats["promoted"] += 1
            stats["promoted_by_edge"][rel] = stats["promoted_by_edge"].get(rel, 0) + 1
            stats["committed"] += 1
        elif proposal.action == "same_as_candidate":
            if _commit_same_as_candidate(driver, state, entity, proposal):
                stats["same_as_candidates"] += 1
                stats["committed"] += 1
            else:
                _skip(stats, "bad_same_as_ref")
        elif proposal.action == "mark_review":
            label = proposal.target_label or entity.guess_label or "Unknown"
            _mark_review(driver, entity, proposal, label)
            stats["review_marked"] += 1
        elif proposal.action == "reject":
            _mark_rejected(driver, entity, proposal)
            stats["rejected"] += 1
        else:
            _skip(stats, "unknown_action")

    logger.info("agentic_graph(%s): %s", state.work.work_id, stats)
    return stats


def _load_work_context(driver: Driver, work_id: str) -> WorkCtx | None:
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
    return WorkCtx(
        work_id=work_id,
        title=rec["title"] or "",
        artist_key=str(picked["key"]) if picked else None,
        artist_name=str(picked["name"]) if picked else None,
    )


def _work_card(driver: Driver, work_id: str) -> str:
    with driver.session() as s:
        rec = s.run(
            f"MATCH (w:{L.WORK} {{{KEY_PROP[L.WORK]}: $wid}}) "
            "RETURN coalesce(w.card_text, w.title, '') AS card",
            wid=work_id,
        ).single()
    return rec["card"] if rec else ""


def _load_entities(driver: Driver, eids: set[str], *, work_id: str,
                   facts_per_entity: int) -> list[EntityCtx]:
    if not eids:
        return []
    with driver.session() as s:
        rows = s.run(
            f"""
            UNWIND $eids AS eid
            MATCH (e:{L.ENTITY}) WHERE elementId(e)=eid
            OPTIONAL MATCH (f:{L.FACT})-[:{E.MENTIONS}]->(e)
            OPTIONAL MATCH (f)-[:{E.EXTRACTED_FROM}]->(d:{L.WEB_DOC})
            // 구조적 작품 앵커링: 같은 Fact가 평가 대상 Work를 MENTIONS하거나,
            // 그 Fact의 WebDoc이 Work에 ABOUT이면 이 Entity는 작품 맥락에 근거한다.
            OPTIONAL MATCH (f)-[:{E.MENTIONS}]->(wf:{L.WORK} {{{KEY_PROP[L.WORK]}: $wid}})
            OPTIONAL MATCH (d)-[:{E.ABOUT}]->(wd:{L.WORK} {{{KEY_PROP[L.WORK]}: $wid}})
            OPTIONAL MATCH (wq:{L.WORK} {{{KEY_PROP[L.WORK]}: $wid}})
                           -[:{E.SPAWNED_QUERY}]->(q:{L.QUERY})
                           -[:{E.RETRIEVED}]->(d)
            RETURN e.{KEY_PROP[L.ENTITY]} AS entity_id,
                   e.name AS name,
                   e.guess_label AS guess_label,
                   e.resolution_status AS resolution_status,
                   e.candidate_label AS candidate_label,
                   max(CASE WHEN wf IS NOT NULL OR wd IS NOT NULL THEN 1 ELSE 0 END)
                     AS work_anchored,
                   max(CASE WHEN wq IS NOT NULL THEN 1 ELSE 0 END) AS collection_anchored,
                   collect(DISTINCT {{
                     fact_id: f.{KEY_PROP[L.FACT]},
                     text: f.text,
                     source_tier: coalesce(f.source_tier, d.source_tier),
                     url: d.url,
                     title: d.title,
                     source_excerpt: f.source_excerpt
                   }}) AS facts
            """,
            eids=list(eids),
            wid=work_id,
        ).data()
    out: list[EntityCtx] = []
    for r in rows:
        raw_facts = [
            f for f in (r.get("facts") or [])
            if f.get("fact_id") and f.get("text")
        ]
        facts = tuple(
            FactCtx(
                fact_id=str(f["fact_id"]),
                text=str(f["text"]),
                source_tier=float(f["source_tier"]) if f.get("source_tier") is not None else None,
                url=f.get("url"),
                title=f.get("title"),
                source_excerpt=f.get("source_excerpt"),
            )
            for f in raw_facts[:facts_per_entity]
        )
        if not r.get("entity_id") or not r.get("name"):
            continue
        if r.get("resolution_status") in {"promoted", "agent_promoted"}:
            continue
        out.append(EntityCtx(
            entity_id=str(r["entity_id"]),
            name=str(r["name"]),
            guess_label=r.get("guess_label"),
            resolution_status=r.get("resolution_status"),
            candidate_label=r.get("candidate_label"),
            facts=facts,
            work_anchored=bool(r.get("work_anchored")),
            collection_anchored=bool(r.get("collection_anchored")),
        ))
    return out


def _load_scope_refs(driver: Driver, eids: set[str]) -> dict[str, str]:
    if not eids:
        return {}
    with driver.session() as s:
        rows = s.run(
            "UNWIND $eids AS eid MATCH (n) WHERE elementId(n)=eid "
            "RETURN labels(n)[0] AS label, properties(n) AS p",
            eids=list(eids),
        ).data()
    refs: dict[str, str] = {}
    for r in rows:
        label = r["label"]
        if label in _GRAPH_OUTPUT_LABELS or label == L.ENTITY:
            continue
        key_prop = KEY_PROP.get(label)
        props = r.get("p") or {}
        key = props.get(key_prop or "")
        if not key:
            continue
        name = _display_name(label, props)
        refs[f"{label}:{key}"] = name[:120]
    return dict(sorted(refs.items())[:80])


def _display_name(label: str, props: dict) -> str:
    for field_name in (
        "title", "name", "artist_name", "agency_name", "genre_name",
        "chart_name", "award_name", "event_name", "trend_name", "fandom_name",
        "theme", "label", "context",
    ):
        value = props.get(field_name)
        if value:
            return f"{value} ({label})"
    return label


# 라벨별 사람이 읽는 이름 필드 — scope_name_index(dedup) 구축용.
_NAME_FIELDS_BY_LABEL: dict[str, tuple[str, ...]] = {
    L.WORK: ("title", "name"),
    L.RELEASE: ("release_title", "title", "name"),
    L.ARTIST: ("name", "artist_name"),
    L.AGENCY: ("agency_name", "name"),
    L.GENRE: ("genre_name", "name"),
    L.CHART: ("chart_name", "name"),
    L.AWARD: ("award_name", "name"),
    L.MEDIA_EVENT: ("event_name", "name"),
    L.VIRAL_TREND: ("trend_name", "name"),
    L.CERTIFICATION: ("name",),
    L.FANDOM: ("fandom_name", "name"),
    L.SOCIAL_THEME: ("theme", "name"),
    L.CAUSE: ("name",),
    L.PUBLIC_USE: ("context", "name"),
}


def _load_scope_name_index(driver: Driver, eids: set[str]) -> dict[tuple[str, str], str]:
    """(label, normalize_text(name)) → key. 승격 시 기존 시드/이전 승격 typed 노드로 dedup
    MERGE 하기 위함(Billboard 변형 수렴·웹 수상→시드 Award 병합). Entity·보고서·Query 제외."""
    if not eids:
        return {}
    with driver.session() as s:
        rows = s.run(
            "UNWIND $eids AS eid MATCH (n) WHERE elementId(n)=eid "
            "RETURN labels(n)[0] AS label, properties(n) AS p",
            eids=list(eids),
        ).data()
    out: dict[tuple[str, str], str] = {}
    for r in rows:
        label = r["label"]
        if label in _GRAPH_OUTPUT_LABELS or label == L.ENTITY:
            continue
        key_prop = KEY_PROP.get(label)
        props = r.get("p") or {}
        key = props.get(key_prop or "")
        if not key:
            continue
        for field_name in _NAME_FIELDS_BY_LABEL.get(label, ()):
            value = props.get(field_name)
            if isinstance(value, str) and value.strip():
                out.setdefault((label, normalize_text(value)), str(key))
    return out


def _resolve_relationship(proposal: GraphProposal, label: str | None) -> str:
    """LLM이 제안한 relationship을 쓰되, 없으면 라벨로 기본 엣지 폴백(하위호환)."""
    rel = (proposal.relationship or "").strip()
    return rel or _DEFAULT_EDGE_FOR_LABEL.get(label or "", "")


def _resolve_anchor(
    state: GraphState, proposal: GraphProposal, rel: str,
) -> tuple[str | None, str | None]:
    """관계의 src 노드(엔티티를 걸 대상)를 정한다. LLM anchor_ref 우선, 없으면 기본 앵커
    (작품 관계→Work, 아티스트 관계→performer Artist). 스키마상 합법 src만."""
    src_labels = EDGE_ENDPOINTS[rel][0]
    ref = (proposal.anchor_ref or "").strip()
    if ref and ":" in ref:
        lbl, key = ref.split(":", 1)
        if lbl in src_labels and (
                ref in state.scope_refs
                or (lbl == L.WORK and key == state.work.work_id)
                or (lbl == L.ARTIST and key == state.work.artist_key)):
            return lbl, key
    if L.WORK in src_labels:
        return L.WORK, state.work.work_id
    if L.ARTIST in src_labels and state.work.artist_key:
        return L.ARTIST, state.work.artist_key
    return None, None


def _validate_promotion(
    state: GraphState,
    entity: EntityCtx,
    proposal: GraphProposal,
    label: str | None,
    rel: str,
    src_label: str | None,
    auto_edges: set[str],
) -> tuple[bool, str]:
    """승격 안전장치(품질 점수가 아님). 의미 판단은 proposer LLM 몫이고, 여기선 검증 가능한
    불변식만 본다: ① 허용 관계인가(blast radius) ② 스키마상 합법 (src→dst)인가(EDGE_ENDPOINTS)
    ③ 실명·근거 Fact·source_excerpt(감사 체인) ④ 작품에 구조적으로 앵커됐나.
    작품(Work) 관계는 work_anchored, 아티스트(Artist) 관계는 work/collection anchored를 요구한다."""
    if not rel or rel not in EDGE_ENDPOINTS:
        return False, "unknown_edge"
    if rel not in auto_edges:
        return False, "edge_not_auto"
    src_ok, dst_ok = EDGE_ENDPOINTS[rel]
    if not label or label not in dst_ok:           # 승격 엔티티 = dst
        return False, "illegal_target_label"
    if src_label is None or src_label not in src_ok:
        return False, "no_valid_anchor"
    name = (proposal.target_name or entity.name or "").strip()
    if _is_generic_name(name):
        return False, "generic_name"
    support = _support_facts(entity, proposal)
    if not support:
        return False, "no_support_fact"
    if not any(f.source_excerpt for f in support):
        return False, "no_source_excerpt"
    if src_label == L.WORK and not entity.work_anchored:
        return False, "not_work_anchored"
    if src_label == L.ARTIST and not (entity.work_anchored or entity.collection_anchored):
        return False, "not_collection_anchored"
    return True, "ok"


def _support_facts(entity: EntityCtx, proposal: GraphProposal) -> list[FactCtx]:
    by_id = {f.fact_id: f for f in entity.facts}
    selected = [by_id[fid] for fid in proposal.evidence_fact_ids if fid in by_id]
    return selected or list(entity.facts)


def _entity_by_surface(state: GraphState, value: str | None) -> EntityCtx | None:
    if not value:
        return None
    norm = normalize_text(value)
    matches = [e for e in state.entities if normalize_text(e.name) == norm]
    return matches[0] if len(matches) == 1 else None


def _commit_promotion(
    driver: Driver,
    state: GraphState,
    entity: EntityCtx,
    proposal: GraphProposal,
    label: str,
    rel: str,
    src_label: str | None,
    src_key: str | None,
) -> None:
    name = (proposal.target_name or entity.name).strip()
    norm = normalize_text(name)
    # dedup: 같은 라벨·정규명의 기존 시드/이전 승격 노드가 있으면 그 키로 MERGE 수렴
    # (Billboard 변형 → 1노드, 웹 수상 → 시드 Award로 병합). 없으면 새 work-scoped 키.
    key = state.scope_name_index.get((label, norm)) or _new_key(label, name, state.work)
    props = _props_for(label, name, state, entity, proposal)
    edge_props = {
        "proposed_by": "music_graph_proposer",
        "confidence": float(proposal.confidence or 0.0),
        "rationale": proposal.rationale[:300],
        "evidence_fact_ids": [f.fact_id for f in _support_facts(entity, proposal)][:5],
    }
    with driver.session() as s:
        _merge_node(s, label, key, props)
        if src_label and src_key:                  # LLM이 판단한 관계(엣지)로 연결
            _merge_edge(s, src_label, src_key, rel, label, key, edge_props)
        _link_supporting_facts(s, entity, proposal, label, key)
        _mark_entity(s, entity.entity_id, {
            "resolution_status": "agent_promoted",
            "merged_into_label": label,
            "merged_into_key": key,
            "promoted_relationship": rel,
            "agent_confidence": float(proposal.confidence or 0.0),
            "agent_rationale": proposal.rationale[:500],
            "needs_review": False,
            "updated_at": _now(),
        })
    # 같은 실행 내 후속 변형이 이 노드로 수렴하도록 name index에 등록.
    state.scope_name_index.setdefault((label, norm), key)
    _embed_label(driver, label)


def _commit_same_as_candidate(
    driver: Driver,
    state: GraphState,
    entity: EntityCtx,
    proposal: GraphProposal,
) -> bool:
    ref = proposal.existing_ref or ""
    if ref not in state.scope_refs or ":" not in ref:
        return False
    label, key = ref.split(":", 1)
    if label not in KEY_PROP:
        return False
    with driver.session() as s:
        _merge_edge(s, L.ENTITY, entity.entity_id, E.SAME_AS_CANDIDATE, label, key, {
            "proposed_by": "music_graph_proposer",
            "confidence": float(proposal.confidence or 0.0),
            "rationale": proposal.rationale[:500],
        })
        _mark_entity(s, entity.entity_id, {
            "resolution_status": "candidate",
            "candidate_label": label,
            "candidate_ref": ref,
            "needs_review": False,
            "agent_confidence": float(proposal.confidence or 0.0),
            "agent_rationale": proposal.rationale[:500],
            "updated_at": _now(),
        })
    return True


def _mark_review(driver: Driver, entity: EntityCtx, proposal: GraphProposal, label: str) -> None:
    with driver.session() as s:
        _mark_entity(s, entity.entity_id, {
            "resolution_status": "review_candidate",
            "candidate_label": label,
            # LLM이 제안한 관계도 남겨, 사람 검토자가 무엇을 어떻게 연결하려 했는지 본다.
            "candidate_relationship": (proposal.relationship or "") or None,
            "needs_review": True,
            "agent_confidence": float(proposal.confidence or 0.0),
            "agent_rationale": proposal.rationale[:500],
            "updated_at": _now(),
        })


def _mark_rejected(driver: Driver, entity: EntityCtx, proposal: GraphProposal) -> None:
    with driver.session() as s:
        _mark_entity(s, entity.entity_id, {
            "resolution_status": "agent_rejected",
            "needs_review": False,
            "agent_confidence": float(proposal.confidence or 0.0),
            "agent_rationale": proposal.rationale[:500],
            "updated_at": _now(),
        })


def _props_for(
    label: str,
    name: str,
    state: GraphState,
    entity: EntityCtx,
    proposal: GraphProposal,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "source_entity_id": entity.entity_id,
        "confidence": float(proposal.confidence or 0.0),
        "source": "music_graph_proposer",
        "description": proposal.rationale[:500],
        "promoted_at": _now(),
    }
    if state.work.artist_name:
        base["artist_name"] = state.work.artist_name
    if label == L.FANDOM:
        base["fandom_name"] = name
    elif label == L.SOCIAL_THEME:
        base.update({"theme": name, "label": name, "work_title": state.work.title})
    elif label == L.CAUSE:
        base.update({"kind": "사회운동·공익 연계"})
    elif label == L.PUBLIC_USE:
        base.update({"context": name, "label": name, "kind": "공적 활용"})
    elif label == L.CHART:
        base["chart_name"] = name
    elif label == L.AWARD:
        base["award_name"] = name
    elif label == L.AGENCY:
        base.update({"agency_name": name, "kind": "기획사·매니지먼트"})
    elif label == L.MEDIA_EVENT:
        base.update({"event_name": name, "kind": "방송·공연·행사"})
    elif label == L.VIRAL_TREND:
        base["trend_name"] = name
    return base


def _new_key(label: str, name: str, work: WorkCtx) -> str:
    norm = normalize_text(name)
    suffix = short_hash(norm)
    if label == L.FANDOM:
        return f"fandom:{work.artist_key or 'unknown_artist'}:{suffix}"
    if label == L.SOCIAL_THEME:
        return f"theme:{work.work_id}:{suffix}"
    if label == L.CAUSE:
        return f"cause:{work.work_id}:{suffix}"
    if label == L.PUBLIC_USE:
        return f"use:{work.work_id}:{suffix}"
    return f"entity:{label.lower()}:{suffix}"


def _merge_node(session, label: str, key: str, props: dict[str, Any]) -> None:
    key_prop = KEY_PROP[label]
    clean = {k: v for k, v in props.items() if v is not None and v != ""}
    card = build_card(label, {**clean, key_prop: key})
    session.run(
        f"MERGE (n:{label} {{{key_prop}: $key}}) "
        "SET n += $props, n.card_text = $card",
        key=key,
        props=clean,
        card=card,
    )


def _merge_edge(session, src_label, src_key, rel, dst_label, dst_key, props=None) -> None:
    sk, dk = KEY_PROP[src_label], KEY_PROP[dst_label]
    session.run(
        f"MATCH (s:{src_label} {{{sk}: $src}}) "
        f"MATCH (d:{dst_label} {{{dk}: $dst}}) "
        f"MERGE (s)-[r:{rel}]->(d) SET r += $props",
        src=src_key,
        dst=dst_key,
        props=props or {},
    )


def _link_supporting_facts(session, entity: EntityCtx, proposal: GraphProposal,
                           label: str, key: str) -> None:
    fact_ids = [f.fact_id for f in _support_facts(entity, proposal)]
    if not fact_ids:
        return
    tk = KEY_PROP[label]
    session.run(
        f"""
        UNWIND $fact_ids AS fid
        MATCH (f:{L.FACT} {{{KEY_PROP[L.FACT]}: fid}})
        MATCH (t:{label} {{{tk}: $key}})
        MERGE (f)-[:{E.MENTIONS}]->(t)
        """,
        fact_ids=fact_ids,
        key=key,
    )


def _mark_entity(session, entity_id: str, props: dict[str, Any]) -> None:
    session.run(
        f"MATCH (e:{L.ENTITY} {{{KEY_PROP[L.ENTITY]}: $eid}}) SET e += $props",
        eid=entity_id,
        props=props,
    )


def _embed_label(driver: Driver, label: str) -> None:
    try:
        from music_copyright.graphflow.graph.vector_index import (
            ensure_vector_index,
            update_embeddings_for_label,
        )

        ensure_vector_index(driver)
        update_embeddings_for_label(driver, label)
    except Exception as exc:  # noqa: BLE001
        logger.debug("agentic graph embedding skip(%s): %s", label, exc)


def _skip(stats: dict[str, Any], reason: str) -> None:
    stats["skipped"] += 1
    stats["skip_reasons"][reason] = stats["skip_reasons"].get(reason, 0) + 1


def _is_generic_name(name: str) -> bool:
    norm = normalize_text(name)
    if not norm or len(norm) < 2:
        return True
    if name.strip().lower() in _GENERIC_STOP:
        return True
    return bool(re.fullmatch(r"(?:19|20)\d{2}|top\s*\d+|\d+\s*위|\d+[.,]?\d*\s*(?:회|명|views?)", name.strip().lower()))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
