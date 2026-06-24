"""Navigator (음악저작권) — 프론티어 선택 + 검색어 합성. real_estate/navigator 미러.

웹검색 확장의 항해사: 그래프에서 '더 캐낼 가치가 있는' 노드(프론티어)를 골라,
그 노드의 고유명사(곡명·아티스트 등)를 포함한 짧은 검색어를 생성한다. LLM
(music_navigator, gpt_mini)이 고르고, 코드가 검증(고유명사 포함·길이·중복)한다.

real_estate와 동일 구조지만 도메인 어휘만 음악(곡/아티스트/차트/수상)으로 교체.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from neo4j import Driver
from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import BaseAgent, music_system_prompt, normalize_text
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.policies import StaticPolicy
from music_copyright.graphflow.prompts import get_prompt
from music_copyright.graphflow.traversal import walk, work_anchor_eid

logger = logging.getLogger("music_copyright.navigator")

_MAX_QUERY_WORDS = 7

# 프론티어로 삼을 라벨 — 더 캐낼 가치가 있는 도메인 노드.
_EXPANDABLE = {L.WORK, L.ARTIST, L.CHART, L.AWARD, L.RIGHTSHOLDER, L.ENTITY}

# 라벨 우선순위 — Platform(스트리밍 지표)은 검색 가치 낮아 억제.
_LABEL_PRIORITY = {
    L.WORK: 1.0, L.CHART: 0.9, L.AWARD: 0.9, L.ARTIST: 0.8,
    L.ENTITY: 0.7, L.RIGHTSHOLDER: 0.5, L.PLATFORM: 0.1,
}


# ───────────────────────────────────────────────────────────
# 프론티어 후보
# ───────────────────────────────────────────────────────────
@dataclass
class FrontierCandidate:
    ref: str                       # "Work:T-..." (label:key)
    label: str
    key: str
    card: str
    proper_nouns: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    score: float = 0.0


def _proper_nouns(label: str, props: dict) -> list[str]:
    """노드에서 검색어에 박을 고유명사 추출 (음악 도메인)."""
    out = []
    for k in ("title", "artist_name", "name", "album", "chart_name", "award_name"):
        v = props.get(k)
        if v and isinstance(v, str):
            out.append(v)
    return list(dict.fromkeys(out))  # dedup, 순서 보존


def _templates(label: str, props: dict) -> list[str]:
    """라벨별 결정적 폴백 검색어 (고유명사 + 평가 관심사)."""
    title = props.get("title")
    artist = props.get("artist_name") or props.get("name")
    name = props.get("name")
    if label == L.WORK and title:
        a = artist or ""
        return [f"{title} {a} 차트", f"{title} 수상", f"{title} 평가 리뷰",
                f"{title} 스트리밍 기록"]
    if label == L.ARTIST and name:
        return [f"{name} 대표곡", f"{name} 수상", f"{name} 평가"]
    if label in (L.CHART, L.AWARD, L.ENTITY) and (name or props.get("chart_name")):
        n = name or props.get("chart_name") or props.get("award_name")
        return [f"{n}"]
    return []


def fetch_props(driver: Driver, eids: list[str]) -> dict[str, dict]:
    """eid → props dict (card_embedding 제외)."""
    if not eids:
        return {}
    with driver.session() as s:
        rows = s.run(
            "UNWIND $eids AS eid MATCH (n) WHERE elementId(n)=eid "
            "RETURN eid, properties(n) AS p", eids=eids,
        ).data()
    out = {}
    for r in rows:
        p = {k: v for k, v in (r["p"] or {}).items() if k != "card_embedding"}
        out[r["eid"]] = p
    return out


def _fact_counts(driver: Driver, eids: list[str]) -> dict[str, tuple[int, int]]:
    """eid → (n_facts, n_queries) — 이미 많이 캔 노드는 후순위로."""
    if not eids:
        return {}
    with driver.session() as s:
        rows = s.run(
            f"""
            UNWIND $eids AS eid MATCH (n) WHERE elementId(n)=eid
            OPTIONAL MATCH (f:{L.FACT})-[:{ 'MENTIONS' }]->(n)
            OPTIONAL MATCH (n)-[:{ 'SPAWNED_QUERY' }]->(q:{L.QUERY})
            RETURN eid, count(DISTINCT f) AS nf, count(DISTINCT q) AS nq
            """, eids=eids,
        ).data()
    return {r["eid"]: (r["nf"], r["nq"]) for r in rows}


def collect_frontier(
    driver: Driver, work_id: str, *, max_hops: int = 2, limit: int = 12,
) -> list[FrontierCandidate]:
    """Work 앵커에서 walk로 프론티어 후보 풀 수집 → 점수순 상위 limit."""
    anchor = work_anchor_eid(driver, work_id)
    if anchor is None:
        return []
    paths = walk(driver, [anchor], StaticPolicy("qa_base"), max_hops=max_hops, k=50)
    tails: dict[str, tuple] = {}
    for p in paths:
        t = p.tail
        if t.label in _EXPANDABLE and t.eid not in tails:
            tails[t.eid] = (t.label, t.key, t.card_text, p.score)
    eids = list(tails)
    props_map = fetch_props(driver, eids)
    counts = _fact_counts(driver, eids)

    cands: list[FrontierCandidate] = []
    for eid, (label, key, card, pscore) in tails.items():
        props = props_map.get(eid, {})
        pn = _proper_nouns(label, props)
        tmpl = _templates(label, props)
        if not pn or not tmpl:
            continue
        nf, nq = counts.get(eid, (0, 0))
        score = (_LABEL_PRIORITY.get(label, 0.3) * pscore
                 / (1 + nf) / (1 + 0.5 * nq))
        cands.append(FrontierCandidate(ref=f"{label}:{key}", label=label, key=key,
                                       card=card, proper_nouns=pn, templates=tmpl,
                                       score=score))
    cands.sort(key=lambda c: (-c.score, c.ref))
    return cands[:limit]


# ───────────────────────────────────────────────────────────
# Navigator 에이전트
# ───────────────────────────────────────────────────────────
class QueryPlan(BaseModel):
    target: str = Field(description="후보 목록의 ref (예: 'Work:T-...')")
    queries: list[str] = Field(description="검색어 1~2개. 고유명사 포함, 6단어 이내")
    why: str = Field(default="", description="선택 이유 1문장")


class NavigatorOutput(BaseModel):
    plans: list[QueryPlan]


def _format_candidates(cands: list[FrontierCandidate]) -> str:
    lines = []
    for c in cands:
        nouns = ", ".join(c.proper_nouns[:4])
        tmpl = " | ".join(c.templates[:3])
        lines.append(f"- {c.ref} | 고유명사: {nouns}\n    카드: {c.card[:80]}\n    템플릿: {tmpl}")
    return "\n".join(lines)


class Navigator(BaseAgent):
    name = "music_navigator"

    def plan_round(
        self,
        work_card: str,
        candidates: list[FrontierCandidate],
        executed: list[str],
        *,
        max_targets: int = 8,
        queries_per_node: int = 2,
        purpose: str | None = None,
    ) -> list[QueryPlan]:
        """프론티어 후보 → 검색 계획. LLM 선택 후 코드 검증/폴백."""
        if not candidates:
            return []
        system = music_system_prompt(get_prompt("music_navigator.system"))
        user_parts = [
            f"[평가 대상]\n{work_card}",
            f"[프론티어 후보 — 이 중에서만 target 선택]\n{_format_candidates(candidates)}",
            f"[이미 실행한 검색어 — 중복 금지]\n" + ("\n".join(f"- {q}" for q in executed[-30:]) or "(없음)"),
            f"최대 {max_targets}개 target, 각 {queries_per_node}개 이내 검색어.",
        ]
        if purpose:
            user_parts.append(f"[이번 검색 목적]\n{purpose}")
        try:
            out: NavigatorOutput = self.complete(
                system=system, user="\n\n".join(user_parts), schema=NavigatorOutput)
            plans = out.plans
        except Exception as exc:  # noqa: BLE001 — LLM 실패 시 템플릿 폴백
            logger.warning("music_navigator 실패, 템플릿 폴백: %s", exc)
            plans = []
        return self._validate(plans, candidates, executed,
                              max_targets=max_targets, queries_per_node=queries_per_node)

    def _validate(self, plans, candidates, executed, *, max_targets, queries_per_node):
        by_ref = {c.ref: c for c in candidates}
        seen = {normalize_text(q) for q in executed}
        out: list[QueryPlan] = []
        # LLM 계획 검증
        for plan in plans[:max_targets]:
            cand = by_ref.get(plan.target)
            if cand is None:
                continue
            valid_qs = []
            for q in plan.queries[:queries_per_node]:
                vq = self._clean_query(q, cand, seen)
                if vq:
                    valid_qs.append(vq)
                    seen.add(normalize_text(vq))
            if valid_qs:
                out.append(QueryPlan(target=plan.target, queries=valid_qs, why=plan.why))
        # LLM이 아무것도 못 내면 상위 후보 템플릿으로 폴백
        if not out:
            for cand in candidates[:max_targets]:
                for t in cand.templates:
                    if normalize_text(t) not in seen:
                        out.append(QueryPlan(target=cand.ref, queries=[t], why="템플릿 폴백"))
                        seen.add(normalize_text(t))
                        break
                if len(out) >= max_targets:
                    break
        return out

    def _clean_query(self, q: str, cand: FrontierCandidate, seen: set) -> str | None:
        """고유명사 포함 + 길이 + 중복 검증. 실패 시 미사용 템플릿으로 대체."""
        q = (q or "").strip()
        has_noun = any(normalize_text(n) in normalize_text(q) for n in cand.proper_nouns)
        too_long = len(q.split()) > _MAX_QUERY_WORDS
        if q and has_noun and not too_long and normalize_text(q) not in seen:
            return q
        # 폴백: 미실행 템플릿
        for t in cand.templates:
            if normalize_text(t) not in seen:
                return t
        return None
