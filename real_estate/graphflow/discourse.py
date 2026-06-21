"""담론 주도 큐레이션 — STORM 관점 발견 + 시뮬레이션 Q&A (PR-3, D17/D18).

고정 EXPAND(navigator→harvester 1패스)를 **관점 주도 다턴 담론**으로 교체한다:

    Perspectivist(관점 발견) → 관점별 [Questioner ↔ Researcher 다턴] → Moderator 공백 스윕

- **Perspectivist**(sonnet): 3축(tenant/esg/commercial)은 고정 척추로 두고, 건물유형에
  맞춘 **추가 관점**을 발견(additive, 상한 N). 검색으로 채울 수 있는 것만(D22).
- **Questioner**(haiku): 관점 페르소나. 관점을 채울 다음 질문 생성, 답을 보고 심화.
- **Researcher**(gpt_mini): 질문을 run_expand(검색 수집)로 답하고, 수집 근거를 읽어
  간결한 답을 회신 — 다음 질문의 심화 입력. 수집 Fact는 그래프=마인드맵에 적재.
- **Moderator**(PR-2 재사용): 관점 스레드 후 커버리지 공백을 측정·메우고 종료 판정.

재사용(불변): walk·harvester·link_mention·validity·composer·StaticPolicy·QA 검색헬퍼.
재현성(D20): 비결정적이라 config.curation.mode == 'discourse'에서만 발동 — 기본 static은
기존 결정 경로 그대로. workflow.expand_node가 게이트. 보고서 구조는 아직 3축 고정
(아웃라인 동적화는 PR-4) — 담론은 '무엇을 수집할지'를 풍부하게 만들 뿐.
"""

from __future__ import annotations

import logging

from neo4j import Driver
from pydantic import BaseModel, Field, field_validator

from real_estate.graphflow.agents.base import BaseAgent, coerce_json
from real_estate.graphflow.composer import (
    _scope_fact_paths,
    format_evidence,
    paths_to_evidence,
)
from real_estate.graphflow.config import get_config
from real_estate.graphflow.graph.schema import NODE_LABELS as L
from real_estate.graphflow.moderator import Moderator
from real_estate.graphflow.prompts import get_prompt
from real_estate.graphflow.traversal import asset_anchor_eid
from infra.agent.tracing import add_score, emit, node_scope

logger = logging.getLogger("graphflow.discourse")

_AXES = ("tenant", "esg", "commercial")     # 고정 척추 (D18)
_BASE_FOCUS = {
    "tenant": "입점 테넌트 구성·임차 안정성·회전율·공실/임대료 동향",
    "esg": "건물 물리 성능·에너지효율/녹색건축 인증·노후·규제",
    "commercial": "입지·상권 건강도·배후수요·유동/매출·개발 호재",
}
_BASE_PERSONA = {                            # Co-STORM 전문가 페르소나 (라운드테이블)
    "tenant": "임차 안정성 분석가",
    "esg": "건물 물리·ESG 성능 평가자",
    "commercial": "입지·상권 전략가",
}


# ───────────────────────────────────────────────────────────
# 스키마
# ───────────────────────────────────────────────────────────
class Perspective(BaseModel):
    parent_axis: str = Field(description="부모 축 — tenant/esg/commercial 중 하나")
    label: str = Field(description="관점 이름 (예: '입주사 신용도', '배후 아파트 입주')")
    focus: str = Field(description="이 관점에서 파고들 핵심 한 문장")
    persona: str = Field(default="", description="이 관점을 대변하는 전문가 페르소나 "
                         "(예: '교통·개발 호재 분석가') — 질문의 시각을 정한다")


class PerspectivesOut(BaseModel):
    perspectives: list[Perspective] = Field(
        default_factory=list,
        description="발견한 추가 관점들 (3축 보충, 상한 N). 없으면 빈 리스트")

    @field_validator("perspectives", mode="before")
    @classmethod
    def _coerce(cls, v):
        return coerce_json(v)


class QuestionTurn(BaseModel):
    question: str = Field(description="이 관점을 채울 다음 검색 질문 (done=true면 빈 문자열 가능)")
    done: bool = Field(description="이 관점이 충분히 다뤄졌거나 검색으로 더 얻을 게 없으면 true")


class OutlineSection(BaseModel):
    axis: str = Field(description="tenant/esg/commercial 중 하나 (3축 척추)")
    points: list[str] = Field(
        description="이 축 섹션에서 다룰 논점(소제목)을 중요도·논리 순서로. "
                    "담론에서 드러난 발견 관점을 부모 축의 논점으로 녹인다")


class OutlineOut(BaseModel):
    sections: list[OutlineSection] = Field(default_factory=list)

    @field_validator("sections", mode="before")
    @classmethod
    def _coerce(cls, v):
        return coerce_json(v)


# ───────────────────────────────────────────────────────────
# 에이전트
# ───────────────────────────────────────────────────────────
class Perspectivist(BaseAgent):
    name = "perspectivist"

    def discover(self, asset_card: str, building_type_summary: str, *,
                 max_perspectives: int = 2) -> list[Perspective]:
        user = (
            f"[평가 대상 자산]\n{asset_card}\n\n"
            f"[건물유형]\n{building_type_summary}\n\n"
            f"[고정 3축]\n- tenant: {_BASE_FOCUS['tenant']}\n"
            f"- esg: {_BASE_FOCUS['esg']}\n- commercial: {_BASE_FOCUS['commercial']}\n\n"
            f"3축은 그대로 두고, 이 자산 특성에 비춰 **추가로 깊이 팔 관점**을 최대 "
            f"{max_perspectives}개 발견하세요(검색으로 채울 수 있는 것만).")
        out: PerspectivesOut = self.complete(
            system=get_prompt("perspectivist.system"), user=user, schema=PerspectivesOut)
        # 부모 축 정규화 + 상한
        valid = [p for p in out.perspectives if p.parent_axis in _AXES]
        return valid[:max_perspectives]


class Questioner(BaseAgent):
    name = "questioner"

    def ask(self, perspective: Perspective, thread: list[dict], *,
            shared_digest: str = "") -> QuestionTurn:
        prior = "\n".join(
            f"Q{i+1}: {t['q']}\nA{i+1}: {t['a']}" for i, t in enumerate(thread)
        ) or "(아직 없음)"
        persona = perspective.persona or perspective.label
        shared_block = (
            f"[다른 전문가들이 지금까지 발견한 것 — 중복 피하고 교차 연관을 파고들어라]\n"
            f"{shared_digest}\n\n" if shared_digest else "")
        user = (
            f"[당신의 전문가 역할] {persona} (관점: {perspective.label}, "
            f"부모축: {perspective.parent_axis})\n"
            f"[초점] {perspective.focus}\n\n"
            + shared_block
            + f"[내 이전 Q&A]\n{prior}\n\n"
            f"이 관점을 채울 다음 질문 하나를 만들거나(이전 답·다른 전문가 발견에서 "
            f"심화), 충분하면 done=true.")
        return self.complete(
            system=get_prompt("questioner.system"), user=user, schema=QuestionTurn)


class Researcher(BaseAgent):
    name = "researcher"

    def research(self, driver: Driver, asset_id: str, question: str,
                 collected_by: str, *, budget: int = 3) -> str:
        """질문을 검색 수집(run_expand)으로 답하고, 수집 근거를 읽어 간결히 회신.

        수집은 기존 harvester(검색→추출→link_mention 오염방지)를 run_expand로 재사용 —
        Researcher의 '전문성'은 도구 라우팅이 아니라 수집 근거의 압축에 있다(D22).
        """
        from real_estate.graphflow.expand import run_expand
        run_expand(driver, asset_id=asset_id, collected_by=collected_by,
                   purpose=question, rounds=1, frontier_max=2, total_query_budget=budget)

        anchor = asset_anchor_eid(driver, asset_id)
        facts = []
        if anchor:
            paths = _scope_fact_paths(driver, asset_id, question, anchor,
                                      n=5, exclude=set(), scope_hops=3)
            facts = paths_to_evidence(driver, paths)
        if not facts:
            return "검색했으나 이 질문에 답할 근거를 찾지 못함."
        resp = self.complete(
            system=get_prompt("researcher.system"),
            user=f"[질문]\n{question}\n\n[수집 근거]\n{format_evidence(facts)}")
        return getattr(resp, "content", str(resp)).strip()


class Outliner(BaseAgent):
    """담론 로그 + 커버리지 → 축별 계층 아웃라인 (STORM 아웃라인 단계, PR-4).

    3축 척추는 유지(D18). 발견 관점의 Q&A에서 드러난 논점을 **부모 축의 소제목**으로
    녹여, writer가 고정 문단이 아니라 담론에서 나온 논점 순서로 집필하게 한다.
    """
    name = "outliner"

    def build(self, asset_card: str, threads: list[dict],
              coverage_summary: str) -> dict[str, list[str]]:
        thread_block = []
        for t in threads:
            qa = "\n".join(f"    Q: {x['q']}\n    A: {x['a']}" for x in t.get("qa", []))
            thread_block.append(
                f"- [{t['axis']}] 관점 '{t['label']}'\n{qa}" if qa
                else f"- [{t['axis']}] 관점 '{t['label']}' (수집 없음)")
        user = (
            f"[평가 대상 자산]\n{asset_card}\n\n"
            f"[관점별 담론 Q&A]\n" + "\n".join(thread_block) + "\n\n"
            f"[커버리지]\n{coverage_summary}\n\n"
            f"3축(tenant/esg/commercial) 각각에 대해 담론에서 드러난 논점을 섹션 "
            f"소제목으로 정리하세요. 발견 관점은 부모 축의 논점으로 녹입니다.")
        out: OutlineOut = self.complete(
            system=get_prompt("outliner.system"), user=user, schema=OutlineOut)
        outline: dict[str, list[str]] = {}
        for sec in out.sections:
            if sec.axis in _AXES and sec.points:
                outline.setdefault(sec.axis, []).extend(p for p in sec.points if p.strip())
        return outline


# ───────────────────────────────────────────────────────────
# 담론 루프 오케스트레이션
# ───────────────────────────────────────────────────────────
def _asset_card(driver: Driver, asset_id: str) -> str:
    with driver.session() as s:
        r = s.run(f"MATCH (a:{L.ASSET} {{asset_id: $aid}}) RETURN a.card_text AS c",
                  aid=asset_id).single()
    return (r["c"] if r else None) or asset_id


def _building_type_summary(bt: dict | None) -> str:
    if not bt:
        return "미분류"
    return f"{bt.get('유형', '미상')} (앵커전략: {bt.get('앵커전략', '미상')})"


def discourse_max_rounds() -> int:
    """담론 사이클 최대 라운드 = config.curation.turns_per_perspective (라운드당 관점 1턴)."""
    return int(get_config().get("curation", {}).get("turns_per_perspective", 2))


# ── STORM 스테이지 (workflow.py가 LangGraph 노드로, run_discourse_expand가 루프로 호출) ──
def perspectives_stage(driver: Driver, asset_id: str, building_type: dict | None, *,
                       perspectives_max: int | None = None) -> list[dict]:
    """관점 발견 (STORM 1단계) → 초기 담론 스레드. 3축 척추 + 발견 관점, 각 빈 qa·done=False."""
    asset_card = _asset_card(driver, asset_id)
    pmax = int(perspectives_max if perspectives_max is not None
               else get_config().get("curation", {}).get("perspectives_max", 2))
    base = [Perspective(parent_axis=ax, label=ax, focus=_BASE_FOCUS[ax],
                        persona=_BASE_PERSONA[ax]) for ax in _AXES]
    discovered = Perspectivist().discover(
        asset_card, _building_type_summary(building_type), max_perspectives=pmax)
    emit("discourse_perspectives", base=len(base), discovered=len(discovered),
         labels=[p.label for p in discovered])
    return [{"axis": p.parent_axis, "label": p.label, "focus": p.focus,
             "persona": p.persona or p.label, "qa": [], "done": False}
            for p in (base + discovered)]


def _shared_digest(threads: list[dict], exclude_idx: int) -> str:
    """다른 전문가들의 최근 발견 1줄 요약 (Co-STORM 공유 대화 — 라운드테이블 가시성)."""
    lines = []
    for j, th in enumerate(threads):
        if j == exclude_idx or not th.get("qa"):
            continue
        last = th["qa"][-1]
        lines.append(f"- [{th.get('persona') or th['label']}] {last['q'][:50]} "
                     f"→ {last['a'][:80]}")
    return "\n".join(lines)


def discourse_round_stage(driver: Driver, asset_id: str, threads: list[dict],
                          collected_by: str, *, research_budget: int | None = None) -> list[dict]:
    """한 담론 라운드 (STORM Q&A) — 각 미완료 관점이 한 턴씩(Questioner→Researcher),
    공유 threads에 append. **Questioner가 다른 전문가들의 발견(공유 다이제스트)을 봄**
    → Co-STORM 진짜 라운드테이블(격리 스레드 아닌 하나의 공유 대화, PR-7)."""
    budget = int(research_budget if research_budget is not None
                 else get_config().get("curation", {}).get("research_budget", 3))
    questioner, researcher = Questioner(), Researcher()
    for i, th in enumerate(threads):
        if th.get("done"):
            continue
        p = Perspective(parent_axis=th["axis"], label=th["label"], focus=th["focus"],
                        persona=th.get("persona", ""))
        qt = questioner.ask(p, th["qa"], shared_digest=_shared_digest(threads, i))
        if qt.done or not qt.question.strip():
            th["done"] = True
            continue
        ans = researcher.research(driver, asset_id, qt.question, collected_by, budget=budget)
        th["qa"].append({"q": qt.question, "a": ans})
    return threads


def moderator_round_stage(driver: Driver, asset_id: str, round_idx: int,
                          max_rounds: int, collected_by: str) -> dict:
    """담론 사이클 컨트롤러 (Co-STORM 모더레이터) — 커버리지 측정 → 공백 질문 mini-EXPAND →
    계속/종료 판정. PR-2 run_moderated_expand의 1라운드를 사이클 노드로 분해."""
    from real_estate.graphflow.coverage import (axis_coverage, axis_fact_samples,
                                                 emit_coverage_scores)
    cur = get_config().get("curation", {})
    cov = axis_coverage(driver, asset_id)
    emit_coverage_scores(cov)
    verdict = Moderator().assess(_asset_card(driver, asset_id), cov,
                                 fact_samples=axis_fact_samples(driver, asset_id),
                                 max_questions=int(cur.get("moderator_max_questions", 3)))
    emit("moderator_verdict", round=round_idx, done=verdict.done,
         n_questions=len(verdict.questions), rationale=verdict.rationale[:160])
    done = bool(verdict.done or not verdict.questions or round_idx >= max_rounds)
    if not done:
        from real_estate.graphflow.expand import run_expand
        per_q = max(1, int(cur.get("moderator_budget_per_round", 6)) // len(verdict.questions))
        for gq in verdict.questions:
            emit("moderator_gap_expand", round=round_idx, axis=gq.axis,
                 question=gq.question[:100])
            run_expand(driver, asset_id=asset_id, collected_by=collected_by,
                       purpose=gq.question, rounds=1, frontier_max=2, total_query_budget=per_q)
    return {"done": done, "questions": len(verdict.questions)}


def outline_stage(driver: Driver, asset_id: str, threads: list[dict]) -> dict:
    """담론 스레드 + 커버리지 → 축별 논점 (STORM 아웃라인, PR-4). writer가 이 순서로 집필."""
    from real_estate.graphflow.coverage import axis_coverage, coverage_summary_text
    outline = Outliner().build(_asset_card(driver, asset_id), threads,
                               coverage_summary_text(axis_coverage(driver, asset_id)))
    emit("discourse_outline", axes=list(outline),
         points=sum(len(v) for v in outline.values()))
    return outline


def _coverage_summary(driver: Driver, asset_id: str) -> dict:
    from real_estate.graphflow.coverage import axis_coverage
    return {ax: {"facts": ac.n_facts, "press": ac.n_press,
                 "uncited": len(ac.uncited), "stale": ac.n_stale}
            for ax, ac in axis_coverage(driver, asset_id).items()}


def run_discourse_expand(
    driver: Driver,
    *,
    asset_id: str,
    building_type: dict | None,
    collected_by: str,
    perspectives_max: int | None = None,
    turns_per_perspective: int | None = None,
    research_budget: int | None = None,
) -> dict:
    """비-LangGraph 드라이버 (try_discourse용) — STORM 스테이지를 평범한 사이클 루프로.

    workflow.py는 동일 스테이지를 LangGraph 노드로 호출(단일 진실, 토폴로지 가시화).
    사이클: perspectives → [discourse_round → moderator]⟲ (done/max_rounds) → outline.
    """
    max_rounds = int(turns_per_perspective if turns_per_perspective is not None
                     else get_config().get("curation", {}).get("turns_per_perspective", 2))
    threads = perspectives_stage(driver, asset_id, building_type,
                                 perspectives_max=perspectives_max)
    rnd = 0
    while rnd < max_rounds:
        rnd += 1
        with node_scope(f"discourse_round_{rnd}"):
            threads = discourse_round_stage(driver, asset_id, threads, collected_by,
                                            research_budget=research_budget)
        with node_scope(f"moderator_round_{rnd}"):
            mod = moderator_round_stage(driver, asset_id, rnd, max_rounds, collected_by)
        if mod["done"]:
            break
    with node_scope("outline"):
        outline = outline_stage(driver, asset_id, threads)

    discovered = [t["label"] for t in threads if t["label"] not in _AXES]
    total_turns = sum(len(t["qa"]) for t in threads)
    add_score("discourse_rounds_run", rnd)
    add_score("discourse_total_turns", total_turns)
    add_score("discourse_outline_points", sum(len(v) for v in outline.values()))
    logger.info("discourse 완료 — 라운드 %d · 관점 %d(발견 %d) · 턴 %d · 논점 %d",
                rnd, len(threads), len(discovered), total_turns,
                sum(len(v) for v in outline.values()))
    return {
        "mode": "discourse",
        "rounds_run": rnd,
        "perspectives_discovered": discovered,
        "threads": [{"axis": t["axis"], "label": t["label"], "turns": len(t["qa"])}
                    for t in threads],
        "outline": outline,
        "final_coverage": _coverage_summary(driver, asset_id),
    }
