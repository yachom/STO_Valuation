"""Navigator (음악저작권) — 프론티어 선택 + 검색어 합성. real_estate/navigator 미러.

웹검색 확장의 항해사: 그래프에서 '더 캐낼 가치가 있는' 노드(프론티어)를 골라,
그 노드의 고유명사(곡명·아티스트 등)를 앵커로 구조화된 조사 과제와 채널별
검색 변형을 생성한다. 다음 라운드에는 질의별 수집 성과를 돌려줘 재계획한다.

real_estate와 동일 구조지만 도메인 어휘만 음악(곡/아티스트/차트/수상)으로 교체.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from infra.agent.tracing import emit
from neo4j import Driver
from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import (
    BaseAgent, music_system_prompt, normalize_text, short_hash)
from music_copyright.graphflow.graph.schema import NODE_LABELS as L
from music_copyright.graphflow.policies import StaticPolicy
from music_copyright.graphflow.prompts import get_prompt
from music_copyright.graphflow.research import QueryOutcome, QueryVariant, ResearchTask
from music_copyright.graphflow.traversal import walk, work_anchor_eid

logger = logging.getLogger("music_copyright.navigator")

_MAX_QUERY_WORDS = 18
_ALLOWED_CHANNELS = {"web", "news", "youtube"}

# 프론티어로 삼을 라벨 — 더 캐낼 가치가 있는 도메인 노드.
_EXPANDABLE = {
    L.WORK, L.ARTIST, L.CHART, L.AWARD, L.RIGHTSHOLDER, L.AGENCY,
    L.FANDOM, L.MEDIA_EVENT, L.VIRAL_TREND, L.ENTITY,
}

# 라벨 우선순위 — Platform(스트리밍 지표)은 검색 가치 낮아 억제.
_LABEL_PRIORITY = {
    L.WORK: 1.0, L.CHART: 0.9, L.AWARD: 0.9, L.ARTIST: 0.8,
    L.ENTITY: 0.7, L.FANDOM: 0.7, L.MEDIA_EVENT: 0.65,
    L.VIRAL_TREND: 0.65, L.AGENCY: 0.55, L.RIGHTSHOLDER: 0.5,
    L.PLATFORM: 0.1,
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
    for k in (
        "title", "artist_name", "name", "album", "chart_name", "award_name",
        "agency_name", "fandom_name", "event_name", "trend_name",
    ):
        v = props.get(k)
        if v and isinstance(v, str):
            out.append(v)
    return list(dict.fromkeys(out))  # dedup, 순서 보존


def _templates(label: str, props: dict) -> list[str]:
    """라벨별 결정적 폴백 검색어 — LLM navigator가 질의를 못 낼 때만 쓰는 최소 안전망.

    축별 일반 평가 차원(차트·수상·평가·스트리밍·사회적 가치)만 쓴다. 과거엔 '문화외교·
    사회공헌·팬덤 기부' 같은 특정 서사(K팝 외교)에 과적합한 사회각도를 강제로 박았는데,
    새 작품·장르엔 편향이라 제거했다(autonomy_decision §3). 사회적 가치의 *구체* 각도는
    LLM이 작품 맥락에서 판단한다 — 폴백은 중립 단어 하나만 둔다.
    """
    title = props.get("title")
    artist = props.get("artist_name") or props.get("name")
    name = props.get("name")
    if label == L.WORK and title:
        a = artist or ""
        return [f"{title} {a} 차트", f"{title} 수상", f"{title} 평가 리뷰",
                f"{title} 스트리밍 기록", f"{title} {a} 사회적 가치"]
    if label == L.ARTIST and name:
        return [f"{name} 대표곡", f"{name} 수상", f"{name} 평가", f"{name} 사회적 영향"]
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
    # 앵커 Work는 검색 확장의 최우선 프론티어다. walk 결과가 이웃만 반환하면
    # 커버/샘플 아티스트가 앞서 나와 작품 자체·주 아티스트 검색이 밀릴 수 있다.
    with driver.session() as s:
        row = s.run(
            f"MATCH (w:{L.WORK} {{work_id: $wid}}) "
            "RETURN elementId(w) AS eid, w.card_text AS card",
            wid=work_id,
        ).single()
    if row:
        tails[row["eid"]] = (L.WORK, work_id, row["card"] or "", 1.0)
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
        card_l = (card or "").lower()
        if label == L.WORK:
            score = max(score * 3.0, 1.2 / (1 + 0.4 * nq))
        elif label == L.ARTIST and "performer" in card_l:
            score = max(score * 2.5, 0.9 / (1 + 0.4 * nq))
        elif label == L.ARTIST and any(t in card_l for t in (
            "커버", "cover", "샘플", "sample",
        )):
            score *= 0.35
        cands.append(FrontierCandidate(ref=f"{label}:{key}", label=label, key=key,
                                       card=card, proper_nouns=pn, templates=tmpl,
                                       score=score))
    def _pin_rank(c: FrontierCandidate) -> int:
        if c.label == L.WORK:
            return 0
        if c.label == L.ARTIST and "performer" in (c.card or "").lower():
            return 1
        return 2

    cands.sort(key=lambda c: (_pin_rank(c), -c.score, c.ref))
    return cands[:limit]


# ───────────────────────────────────────────────────────────
# Navigator 에이전트
# ───────────────────────────────────────────────────────────
class NavigatorOutput(BaseModel):
    tasks: list[ResearchTask] = Field(default_factory=list)


# 기존 내부 import 호환. 새 코드는 ResearchTask를 직접 사용한다.
QueryPlan = ResearchTask


def _format_candidates(cands: list[FrontierCandidate]) -> str:
    lines = []
    for c in cands:
        nouns = ", ".join(c.proper_nouns[:4])
        lines.append(f"- {c.ref} | 고유명사: {nouns}\n    카드: {c.card[:160]}")
    return "\n".join(lines)


def _format_feedback(outcomes: list[QueryOutcome]) -> str:
    if not outcomes:
        return "(첫 라운드 — 성과 피드백 없음)"
    lines = []
    for o in outcomes[-16:]:
        skipped = ", ".join(f"{k}={v}" for k, v in sorted(o.skip_reasons.items())) or "없음"
        lines.append(
            f"- [{o.axis}/{o.strategy}] {o.query} | utility={o.utility:.3f}, "
            f"문서={o.docs_ingested}/{o.docs_selected}, grounded_fact={o.grounded_facts}, "
            f"새_fact={o.facts_new}, 폐기={skipped}"
        )
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
        coverage: str | None = None,
        feedback: list[QueryOutcome] | None = None,
        round_no: int = 1,
    ) -> list[ResearchTask]:
        """프론티어와 수집 성과 → 구조화된 조사 과제. LLM 제안 후 안전 검증."""
        if not candidates:
            return []
        system = music_system_prompt(get_prompt("music_navigator.system"))
        user_parts = [
            f"[평가 대상]\n{work_card}",
            f"[프론티어 후보 — 이 중에서만 target 선택]\n{_format_candidates(candidates)}",
            f"[현재 축별 그래프 커버리지]\n{coverage or '(없음)'}",
            f"[이미 실행한 검색어 — 중복 금지]\n" + ("\n".join(f"- {q}" for q in executed[-30:]) or "(없음)"),
            f"[직전 질의 성과 — 낮은 성과 원인을 피해 재계획]\n{_format_feedback(feedback or [])}",
            f"라운드 {round_no}. 최대 {max_targets}개 ResearchTask, 각 task당 "
            f"QueryVariant {queries_per_node}개 이내.",
        ]
        if purpose:
            user_parts.append(f"[이번 검색 목적]\n{purpose}")
        try:
            out: NavigatorOutput = self.complete(
                system=system, user="\n\n".join(user_parts), schema=NavigatorOutput)
            tasks = out.tasks
        except Exception as exc:  # noqa: BLE001 — LLM 실패 시 템플릿 폴백
            logger.warning("music_navigator 실패, 템플릿 폴백: %s", exc)
            tasks = []
        validated = self._validate(
            tasks, candidates, executed, max_targets=max_targets,
            queries_per_node=queries_per_node, purpose=purpose)
        validated = self._ensure_social_floor(
            validated, candidates, executed,
            queries_per_node=queries_per_node, purpose=purpose)
        emit(
            "research_plan",
            round=round_no,
            purpose=(purpose or "")[:500],
            tasks=[t.model_dump(exclude={"queries"}) for t in validated],
            feedback_count=len(feedback or []),
            fallback=not bool(tasks),
        )
        return validated

    def _validate(self, tasks, candidates, executed, *, max_targets, queries_per_node,
                  purpose: str | None = None) -> list[ResearchTask]:
        by_ref = {c.ref: c for c in candidates}
        seen = {normalize_text(q) for q in executed}
        out: list[ResearchTask] = []
        for task in tasks[:max_targets]:
            target = self._compatible_target(task, candidates)
            cand = by_ref.get(target)
            if cand is None:
                continue
            variants: list[QueryVariant] = []
            for variant in task.query_variants()[:queries_per_node]:
                text = self._clean_query(variant.text, cand, seen)
                if not text:
                    continue
                channels = list(dict.fromkeys(
                    c for c in variant.channels if c in _ALLOWED_CHANNELS)) or ["web", "news"]
                variants.append(QueryVariant(
                    text=text,
                    channels=channels,
                    strategy=variant.strategy,
                    expected_signal=variant.expected_signal,
                ))
                seen.add(normalize_text(text))
            if not variants:
                continue
            question = task.question.strip() or purpose or task.evidence_goal.strip()
            task_id = short_hash(
                f"{target}|{task.axis}|{normalize_text(question)}")
            out.append(ResearchTask(
                task_id=task_id,
                target=target,
                axis=task.axis,
                question=question,
                evidence_goal=task.evidence_goal.strip(),
                evidence_type=task.evidence_type,
                source_preferences=task.source_preferences[:5],
                variants=variants,
                why=task.why,
            ))
        if not out:
            out = self._fallback_tasks(
                candidates, seen, max_targets=max_targets,
                queries_per_node=queries_per_node, purpose=purpose)
        return out[:max_targets]

    @staticmethod
    def _compatible_target(task: ResearchTask,
                           candidates: list[FrontierCandidate]) -> str:
        """Keep direct-work and artist-context evidence semantics explicit."""
        by_ref = {c.ref: c for c in candidates}
        current = by_ref.get(task.target)
        direct_types = {"direct_work", "metric", "critical_reception", "public_use"}
        if task.evidence_type in direct_types and (not current or current.label != L.WORK):
            work = next((c for c in candidates if c.label == L.WORK), None)
            return work.ref if work else task.target
        if task.evidence_type == "artist_context" and (
                not current or current.label not in {L.ARTIST, L.FANDOM, L.AGENCY}):
            artist = next((c for c in candidates if c.label == L.ARTIST and
                           "performer" in (c.card or "").lower()), None)
            return artist.ref if artist else task.target
        return task.target

    def _clean_query(self, q: str, cand: FrontierCandidate, seen: set) -> str | None:
        """질의 의도를 보존하면서 빠진 대상 앵커만 보강한다."""
        q = (q or "").strip()
        if not q:
            return None
        norm_q = normalize_text(q)
        required = cand.proper_nouns[:2] if cand.label == L.WORK else cand.proper_nouns[:1]
        missing = [n for n in required if normalize_text(n) not in norm_q]
        if missing:
            q = " ".join(f'"{n}"' for n in missing) + " " + q
            norm_q = normalize_text(q)
        if len(q.split()) > _MAX_QUERY_WORDS or len(q) > 180 or norm_q in seen:
            return None
        return q

    def _fallback_tasks(self, candidates, seen, *, max_targets, queries_per_node,
                        purpose: str | None) -> list[ResearchTask]:
        """LLM 장애 시에만 쓰는 최소 안전 경로. 정상 계획을 조타하지 않는다."""
        out: list[ResearchTask] = []
        axes = ("influence", "popularity", "sustainability", "social_value")
        for idx, cand in enumerate(candidates[:max_targets]):
            variants = []
            for raw in cand.templates:
                q = self._clean_query(raw, cand, seen)
                if not q:
                    continue
                variants.append(QueryVariant(
                    text=q, channels=["web", "news"], strategy="discovery",
                    expected_signal=purpose or "평가 가능한 직접 근거",
                ))
                seen.add(normalize_text(q))
                if len(variants) >= queries_per_node:
                    break
            if not variants:
                continue
            axis = axes[idx % len(axes)]
            question = purpose or f"{cand.proper_nouns[0]}의 {axis} 직접 근거는 무엇인가?"
            out.append(ResearchTask(
                task_id=short_hash(f"{cand.ref}|{axis}|{normalize_text(question)}"),
                target=cand.ref, axis=axis, question=question,
                evidence_goal="검증 가능한 직접 출처 1건 이상",
                evidence_type="discovery", variants=variants,
                why="Navigator 장애 시 최소 수집 폴백",
            ))
        return out

    # ── 사회축 최소 보장 (social floor) ──────────────────────────
    # 상업 프론티어(차트·수상·평가)가 LLM 계획을 지배하면 social_value 질의가 매 라운드
    # 0건이 돼 사회 데이터가 구조적으로 굶는다. 정상 계획에 social_value 과제가 임계 미만
    # 이면 결정적 사회 과제 1건을 보충한다. 토큰 사전이 아니라 (작품/아티스트 고유명사 +
    # axes.py의 social_value Axis 의도)로 합성한다 — 사회 의도의 단일 출처는 axes.py.
    _SOCIAL_AXIS = "social_value"

    def _ensure_social_floor(
        self, tasks: list[ResearchTask], candidates: list[FrontierCandidate],
        executed: list[str], *, queries_per_node: int, purpose: str | None,
        min_tasks: int = 1,
    ) -> list[ResearchTask]:
        """validated 계획에 social_value 과제가 min_tasks 미만이면 사회 과제를 보충."""
        have = sum(1 for t in tasks if t.axis == self._SOCIAL_AXIS)
        if have >= min_tasks:
            return tasks
        floor = self._social_floor_task(
            candidates, executed, tasks,
            queries_per_node=queries_per_node, purpose=purpose)
        return tasks + [floor] if floor is not None else tasks

    def _social_floor_task(
        self, candidates: list[FrontierCandidate], executed: list[str],
        existing: list[ResearchTask], *, queries_per_node: int, purpose: str | None,
    ) -> ResearchTask | None:
        """작품(우선) 또는 performer 아티스트를 앵커로 한 결정적 social_value 과제."""
        from music_copyright.graphflow.axes import axis_by_key
        cand = (next((c for c in candidates if c.label == L.WORK), None)
                or next((c for c in candidates if c.label == L.ARTIST and
                         "performer" in (c.card or "").lower()), None)
                or (candidates[0] if candidates else None))
        if cand is None or not cand.proper_nouns:
            return None
        seen = {normalize_text(q) for q in executed}
        for t in existing or []:
            for v in t.query_variants():
                seen.add(normalize_text(v.text))
        ax = axis_by_key(self._SOCIAL_AXIS)
        signal = (ax.writer_focus if ax else "") or "사회적 가치 직접 근거"
        variants: list[QueryVariant] = []
        for raw in self._social_query_seeds(cand):
            text = self._clean_query(raw, cand, seen)
            if not text:
                continue
            variants.append(QueryVariant(
                text=text, channels=["web", "news"], strategy="context",
                expected_signal=signal))
            seen.add(normalize_text(text))
            if len(variants) >= max(1, queries_per_node):
                break
        if not variants:
            return None
        etype = "public_use" if cand.label == L.WORK else "artist_context"
        question = (purpose.strip() if purpose and "\n" not in purpose
                    and len(purpose) <= 120 else
                    f"{cand.proper_nouns[0]}의 사회적 가치(메시지·사회운동·공익·대표성·"
                    "문화외교) 근거는 무엇인가?")
        return ResearchTask(
            task_id=short_hash(
                f"{cand.ref}|{self._SOCIAL_AXIS}|{normalize_text(question)}"),
            target=cand.ref, axis=self._SOCIAL_AXIS, question=question,
            evidence_goal=(ax.writer_focus if ax else
                           "사회적 가치 검증 가능한 직접 근거 1건 이상"),
            evidence_type=etype, variants=variants,
            why="사회축 최소 보장 — 상업 프론티어 편향으로 social_value가 굶지 않게")

    @staticmethod
    def _social_query_seeds(cand: FrontierCandidate) -> list[str]:
        """결정적 사회 질의 씨앗 — _templates의 중립 사회 표현과 동일 어휘(새 사전 아님)."""
        name = cand.proper_nouns[0] if cand.proper_nouns else ""
        if not name:
            return []
        if cand.label == L.WORK:
            artist = cand.proper_nouns[1] if len(cand.proper_nouns) > 1 else ""
            return [f"{name} {artist} 사회적 가치".strip()]
        return [f"{name} 사회적 영향"]
